//! Service programs: `program list|show|save|visibility|delete|revisions|copy|draft`.
//!
//! Projections follow `shared/schemas/service/programs.schema.json`. Server
//! field names are kept verbatim (snake case). Program bytes are never
//! normalized: what `save` reads is what the service stores, and `show`
//! returns or writes the stored bytes exactly. The Bun twin is
//! `cli/bun/src/core/service/programs.ts`; both are pinned by
//! `cli/conformance/cases/service/programs/`.
use super::fs;
use super::http::{Request, StreamOpen, encode_segment};
use super::program_ref::{self, valid_rev, valid_slug};
use super::sse::{SseItem, StreamEnd};
use super::workspace::{self, Role};
use super::{Context, Gate};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fmt::Write as _;
use std::path::Path;

/// The service's program size limit (`MAX_CONTENT_BYTES`, 256 KiB), enforced
/// before any request.
pub const MAX_PROGRAM_BYTES: u64 = 256 * 1024;
/// Revision messages: the service trims them and allows 160 characters.
const MAX_MESSAGE_CHARS: usize = 160;
/// A draft request sentence.
const MAX_SENTENCE_CHARS: usize = 4096;
/// Largest listing the closed result schema admits.
const MAX_ITEMS: usize = 1000;
/// `common.schema.json#/$defs/text` maximum.
const MAX_TEXT_CHARS: usize = 1_048_576;
/// Largest integer both products represent exactly.
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.invocation.operation.as_str() {
        "program.list" => list(context),
        "program.show" => show(context),
        "program.save" => save(context),
        "program.visibility" => visibility(context),
        "program.delete" => delete(context),
        "program.revisions" => revisions(context),
        "program.copy" => copy(context),
        "program.draft" => draft(context),
        _ => context.not_implemented(),
    }
}

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn protocol(reason: impl Into<String>) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid).with_detail("reason", reason.into())
}

/// Program text as `common.schema.json#/$defs/text` admits it: at most
/// 1,048,576 code points, no C0 control other than TAB, LF and CR, no DEL.
pub fn is_text(value: &str) -> bool {
    value.chars().count() <= MAX_TEXT_CHARS
        && !value.chars().any(|character| {
            (character <= '\u{1f}' && !matches!(character, '\t' | '\n' | '\r'))
                || character == '\u{7f}'
        })
}

/// Request index of `GET /programs/{slug}/revisions` in save, visibility and
/// delete (the OWNER/SLUG check).
const OWNER_CHECK: usize = 1;
/// Request index of `GET /programs?workspace=ORG` in save, visibility and
/// delete (the `--org` confirmation).
const ORGANIZATION_CHECK: usize = 2;

/// `<SLUG>`, parsed before any request: a bare slug, or `OWNER/SLUG` when
/// OWNER is the caller (checked by `confirm_own_slug`).
fn slug_reference(context: &Context<'_>) -> Result<program_ref::ProgramRef, RunnerError> {
    program_ref::own_slug_argument(context)
}

/// The `<SLUG>` of an owner-scoped verb and the organization it acts in:
/// with `--org ORG`, OWNER (when given) must be ORG and the plan names the
/// organization; without it, `OWNER/SLUG` must be the caller (`creates` is a
/// first save).
fn scoped_slug(
    context: &mut Context<'_>,
    creates: bool,
) -> Result<(program_ref::ProgramRef, Option<String>), RunnerError> {
    let reference = slug_reference(context)?;
    let organization = workspace::organization_option(context, "--org")?;
    match &organization {
        None => {
            program_ref::confirm_own_slug(context, &reference, OWNER_CHECK, creates)?;
        }
        Some(organization) => {
            let given = context.argument("SLUG").unwrap_or_default().to_owned();
            workspace::require_same_organization(
                context,
                &reference.owner,
                &reference.slug,
                organization,
                &given,
            )?;
            // A service that ignores `?workspace=` must never receive the
            // write.
            workspace::confirm_organization(context, ORGANIZATION_CHECK, organization)?;
            context.organization = Some(organization.clone());
        }
    }
    Ok((reference, organization))
}

/// Sends an organization-scoped request; organization failures name the
/// organization and the role `what` needs. With `slug`, a plain 404 is that
/// program missing from the organization (or the caller not being a
/// member); without it, every 404 is the organization.
fn send_scoped(
    context: &mut Context<'_>,
    request: &Request,
    organization: Option<&str>,
    role: Role,
    what: &str,
    slug: Option<&str>,
) -> Result<Map<String, Value>, RunnerError> {
    let error = match context.send(request) {
        Ok(response) => return response.json_object(),
        Err(error) => error,
    };
    let Some(organization) = organization else {
        return Err(error);
    };
    let explained = workspace::organization_failure(
        context,
        error,
        organization,
        role,
        what,
        "--org",
        slug.is_none(),
    );
    Err(match slug {
        Some(slug)
            if explained.code == ErrorCode::ServiceResourceNotFound && unexplained(&explained) =>
        {
            program_ref::missing_in_organization(context, organization, slug, explained)
        }
        _ => explained,
    })
}

/// A not-found error no handler has named a resource for yet.
fn unexplained(error: &RunnerError) -> bool {
    error
        .details
        .as_ref()
        .is_none_or(|details| !details.contains_key("resource"))
}

/// Checks an `--output-file` target before any request (it must not exist
/// and its directory must), so a paid or remote call never ends in a local
/// refusal. The file itself is created only after success.
fn preflight_output(cwd: &Path, value: &str) -> Result<(), RunnerError> {
    let path = if Path::new(value).is_absolute() {
        Path::new(value).to_path_buf()
    } else {
        cwd.join(value)
    };
    if std::fs::symlink_metadata(&path).is_ok() {
        return Err(invalid(format!(
            "output file {value_quoted} already exists; choose a new path",
            value_quoted = crate::error::quote(value)
        )));
    }
    if !path.parent().is_some_and(Path::is_dir) {
        return Err(invalid(format!(
            "the directory for output file {value_quoted} does not exist",
            value_quoted = crate::error::quote(value)
        )));
    }
    Ok(())
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

/// `common.schema.json#/$defs/writtenFile`: the bytes are written to
/// `--output-file` when given; otherwise `content` carries them when they
/// are text. Local paths are never reported.
fn written_file(context: &Context<'_>, bytes: &[u8]) -> Result<Value, RunnerError> {
    let mut file = Map::new();
    file.insert("bytes".into(), json!(bytes.len()));
    file.insert("sha256".into(), json!(sha256_hex(bytes)));
    if let Some(target) = context.option("--output-file") {
        fs::write_new_file(&context.system.current_dir, target, bytes)?;
        file.insert("written".into(), json!(true));
    } else {
        file.insert("written".into(), json!(false));
        if let Some(text) = std::str::from_utf8(bytes).ok().filter(|text| is_text(text)) {
            file.insert("content".into(), json!(text));
        }
    }
    Ok(Value::Object(file))
}

fn integer(value: &Value, minimum: u64) -> Option<u64> {
    value
        .as_u64()
        .filter(|number| *number >= minimum && *number <= MAX_SAFE_INTEGER)
}

/// Control characters become spaces and the text is cut to 1024 code
/// points; a blank message is dropped. Messages are labels, so a stray
/// control character never fails a whole listing.
fn message(value: &Value) -> Result<Option<String>, RunnerError> {
    match value {
        Value::Null => Ok(None),
        Value::String(text) => {
            let spaced = text
                .chars()
                .map(|character| {
                    if character <= '\u{1f}' || character == '\u{7f}' {
                        ' '
                    } else {
                        character
                    }
                })
                .take(1024)
                .collect::<String>();
            Ok((!spaced.chars().all(|character| character == ' ')).then_some(spaced))
        }
        _ => Err(protocol("program field `message` is invalid")),
    }
}

/// Projects one service program record. `visibility` is required unless
/// the record is a revision; `content` is kept only when asked for and text.
fn project(
    record: &Value,
    with_visibility: bool,
    with_content: bool,
) -> Result<Value, RunnerError> {
    let field = |name: &str| protocol(format!("program field `{name}` is invalid"));
    let Some(object) = record.as_object() else {
        return Err(protocol("the service returned an invalid program record"));
    };
    let get = |name: &str| object.get(name).unwrap_or(&Value::Null);
    let mut out = Map::new();
    let owner = get("owner")
        .as_str()
        .filter(|owner| program_ref::valid_owner(owner))
        .ok_or_else(|| field("owner"))?;
    out.insert("owner".into(), json!(owner));
    let slug = get("slug")
        .as_str()
        .filter(|slug| valid_slug(slug))
        .ok_or_else(|| field("slug"))?;
    out.insert("slug".into(), json!(slug));
    if with_visibility {
        let visibility = get("visibility")
            .as_str()
            .filter(|value| matches!(*value, "public" | "private"))
            .ok_or_else(|| field("visibility"))?;
        out.insert("visibility".into(), json!(visibility));
    }
    out.insert(
        "rev".into(),
        json!(integer(get("rev"), 1).ok_or_else(|| field("rev"))?),
    );
    for name in ["rev_id", "commit_id"] {
        let value = get(name)
            .as_str()
            .filter(|value| valid_rev(value))
            .ok_or_else(|| field(name))?;
        out.insert(name.into(), json!(value));
    }
    // The pinned reference, ready for `run submit --from` and `job contract
    // attach`.
    let reference = program_ref::ref_of(owner, slug, out["rev_id"].as_str().unwrap_or_default());
    out.insert("ref".into(), json!(reference));
    let parent = match get("parent_commit_id") {
        Value::Null => Value::Null,
        Value::String(value) if valid_rev(value) => json!(value),
        _ => return Err(field("parent_commit_id")),
    };
    out.insert("parent_commit_id".into(), parent);
    out.insert(
        "updated_at".into(),
        json!(integer(get("updated_at"), 0).ok_or_else(|| field("updated_at"))?),
    );
    super::render::add_iso(&mut out, &["updated_at"]);
    if let Some(text) = message(get("message"))? {
        out.insert("message".into(), json!(text));
    }
    // Revisions carry neither; program records may name their namespace
    // kind and source.
    if with_visibility {
        workspace::project_workspace(object, &mut out)?;
    }
    if with_content {
        match get("content") {
            Value::String(text) if is_text(text) => {
                out.insert("content".into(), json!(text));
            }
            Value::String(_) | Value::Null => {}
            _ => return Err(field("content")),
        }
    }
    Ok(Value::Object(out))
}

fn items<'a>(body: &'a Map<String, Value>, key: &str) -> Result<&'a Vec<Value>, RunnerError> {
    let values = body
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| protocol(format!("the service response has no `{key}` array")))?;
    if values.len() > MAX_ITEMS {
        return Err(
            RunnerError::catalog(ErrorCode::ServiceResponseTooLarge).with_detail(
                "reason",
                format!("the service returned more than {MAX_ITEMS} {key}"),
            ),
        );
    }
    Ok(values)
}

fn name_of(program: &Value) -> String {
    format!(
        "{}/{}",
        program["owner"].as_str().unwrap_or_default(),
        program["slug"].as_str().unwrap_or_default()
    )
}

fn list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let with_content = context.flag("--with-content");
    let organization = workspace::organization_option(context, "--org")?;
    let request = workspace::scoped(
        Request::from_manifest(context.operation, 0, "/programs"),
        organization.as_deref(),
    );
    let body = send_scoped(
        context,
        &request,
        organization.as_deref(),
        Role::Reader,
        "listing its programs",
        None,
    )?;
    if let Some(organization) = &organization {
        workspace::require_echo(&body, organization)?;
    }
    let programs = items(&body, "programs")?
        .iter()
        .map(|record| project(record, true, with_content))
        .collect::<Result<Vec<_>, _>>()?;
    let mut human = String::new();
    if programs.is_empty() {
        match &organization {
            None => human.push_str("No saved programs.\n"),
            Some(organization) => {
                let _ = writeln!(human, "No programs in organization {organization}.");
            }
        }
    }
    for program in &programs {
        let _ = writeln!(
            human,
            "{}  rev {}  {}  rev_id={}  ref={}{}",
            name_of(program),
            program["rev"],
            program["visibility"].as_str().unwrap_or_default(),
            program["rev_id"].as_str().unwrap_or_default(),
            program["ref"].as_str().unwrap_or_default(),
            workspace::workspace_columns(program)
        );
    }
    context.human = Some(human);
    Ok(json!({ "programs": programs }))
}

/// A bare SLUG that is not one of the caller's programs: when the caller's
/// program list (manifest request 2) has one slug within two edits, the
/// suggestion is the same command with that slug. The listing is advisory: a
/// failed listing keeps `error`.
fn nearest_own_program(
    context: &mut Context<'_>,
    error: RunnerError,
    given: &str,
    slug: &str,
    organization: Option<&str>,
) -> RunnerError {
    let request = workspace::scoped(
        Request::from_manifest(context.operation, 2, "/programs"),
        organization,
    )
    .class(super::http::TransportClass::Control);
    let Ok(body) = context
        .send(&request)
        .and_then(|response| response.json_object())
    else {
        return error;
    };
    let slugs = body
        .get("programs")
        .and_then(Value::as_array)
        .map(|programs| {
            programs
                .iter()
                .filter_map(|program| program["slug"].as_str())
                .filter(|candidate| program_ref::valid_slug(candidate))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let Some(near) = super::did_you_mean(slug, slugs.iter().copied()) else {
        return error;
    };
    let near = near.to_owned();
    let mut error = error;
    if let Some(details) = error.details.as_mut() {
        if let Some(Value::String(reason)) = details.get_mut("reason") {
            let _ = write!(reason, "; did you mean {near}?");
        }
    }
    // Only the slug changes: an OWNER/ prefix is kept as given.
    let fixed = given.split_once('/').map_or_else(
        || given.replacen(slug, &near, 1),
        |(owner, rest)| format!("{owner}/{}", rest.replacen(slug, &near, 1)),
    );
    let whose = organization.map_or_else(
        || "you have".to_owned(),
        |organization| format!("organization {organization} has"),
    );
    context.corrected(
        error,
        &format!("Run `{{command}}`: {near} is the closest program {whose}"),
        context.argv_with_argument(given, &fixed),
    )
}

fn show(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let given = context
        .argument("OWNER/SLUG[@REV]")
        .unwrap_or_default()
        .to_owned();
    let mut reference = program_ref::parse_own_allowed_with(&given, true)?;
    let organization = workspace::organization_option(context, "--org")?;
    if let Some(organization) = &organization {
        // With --org a bare SLUG is the organization's program; OWNER must
        // be it.
        workspace::require_same_organization(
            context,
            &reference.owner,
            &reference.slug,
            organization,
            &given,
        )?;
        organization.clone_into(&mut reference.owner);
    }
    if let Some(target) = context.option("--output-file") {
        preflight_output(&context.system.current_dir, target)?;
    }
    // A bare SLUG is the caller's own program and a numeric @N is resolved to
    // its rev_id (manifest request 1).
    let by_number = reference.rev_number.is_some();
    let bare = reference.owner.is_empty();
    let resolved = if by_number {
        program_ref::resolve_rev_number(context, &mut reference, 1, organization.as_deref())
    } else {
        program_ref::fill_owner(context, &mut reference, 1)
    };
    if let Err(error) = resolved {
        return Err(match &organization {
            Some(organization) => workspace::organization_failure(
                context,
                error,
                organization,
                Role::Reader,
                "reading its programs",
                "--org",
                false,
            ),
            None if bare && error.code == ErrorCode::ServiceResourceNotFound => {
                nearest_own_program(context, error, &given, &reference.slug, None)
            }
            None => error,
        });
    }
    let mut path = format!(
        "/p/{}/{}",
        encode_segment(&reference.owner),
        encode_segment(&reference.slug)
    );
    if let Some(rev) = &reference.rev {
        path.push('@');
        path.push_str(rev);
    }
    let mut request = Request::from_manifest(context.operation, 0, path);
    // `program show` reads anonymously when no credential is configured
    // (public programs); a configured key is always sent, so owners see
    // private ones.
    request.bearer = context.has_credential();
    let body = match context.send(&request) {
        Ok(response) => response.json_object()?,
        // The organization's program is missing, or private to members.
        Err(error)
            if organization.is_some() && error.code == ErrorCode::ServiceResourceNotFound =>
        {
            let organization = organization.as_deref().unwrap_or_default();
            let missing =
                program_ref::missing_in_organization(context, organization, &reference.slug, error);
            return Err(nearest_own_program(
                context,
                missing,
                &given,
                &reference.slug,
                Some(organization),
            ));
        }
        Err(error) => return Err(error),
    };
    let record = body.get("program").cloned().unwrap_or(Value::Null);
    let program = project(&record, true, false)?;
    if !program["owner"]
        .as_str()
        .is_some_and(|owner| owner.eq_ignore_ascii_case(&reference.owner))
        || program["slug"] != reference.slug.as_str()
        || reference
            .rev
            .as_ref()
            .is_some_and(|rev| program["rev_id"] != rev.as_str())
    {
        return Err(protocol("the service returned a different program"));
    }
    let source = record["content"]
        .as_str()
        .ok_or_else(|| protocol("program field `content` is invalid"))?;
    let is_owner = body
        .get("is_owner")
        .and_then(Value::as_bool)
        .ok_or_else(|| protocol("the service response has no `is_owner` flag"))?;
    let file = written_file(context, source.as_bytes())?;
    context.human = Some(match file["content"].as_str() {
        Some(text) => text.to_owned(),
        None => file_line(&name_of(&program), &file, context.option("--output-file")),
    });
    if by_number && context.mode == crate::OutputMode::Human {
        let note = format!(
            "Resolved {} to {} (rev {}).\n",
            human_safe_scalar(&given),
            program["ref"].as_str().unwrap_or_default(),
            program["rev"]
        );
        let _ = std::io::Write::write_all(context.err, note.as_bytes());
    }
    if context.mode == crate::OutputMode::Human {
        let notes = workspace::workspace_notes(&program);
        let _ = std::io::Write::write_all(context.err, notes.as_bytes());
    }
    Ok(json!({ "program": program, "is_owner": is_owner, "file": file }))
}

fn file_line(label: &str, file: &Value, target: Option<&str>) -> String {
    let bytes = &file["bytes"];
    let sha = file["sha256"].as_str().unwrap_or_default();
    match target {
        Some(target) => format!(
            "Wrote {bytes} bytes to {} (sha256 {sha})\n",
            human_safe_scalar(target)
        ),
        None => format!(
            "{label} is {bytes} bytes (sha256 {sha}) and not plain text; write it with --output-file FILE\n"
        ),
    }
}

fn save(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let reference = slug_reference(context)?;
    workspace::organization_option(context, "--org")?;
    let source = context.argument("FILE").unwrap_or_default().to_owned();
    let text = fs::read_text(
        &context.system.current_dir,
        &source,
        MAX_PROGRAM_BYTES,
        "FILE",
    )?;
    if text.is_empty() {
        return Err(invalid(format!(
            "FILE {source_quoted} is empty; a program needs content",
            source_quoted = crate::error::quote(&source)
        )));
    }
    let mut body = Map::new();
    body.insert("content".into(), json!(text));
    if let Some(text) = context.option("--message") {
        let trimmed = text.trim_matches(|c| matches!(c, ' ' | '\t' | '\n' | '\r'));
        if !super::render::valid_text(trimmed, MAX_MESSAGE_CHARS) {
            return Err(invalid(format!(
                "--message must be 1 to {MAX_MESSAGE_CHARS} characters without control characters"
            )));
        }
        body.insert("message".into(), json!(trimmed));
    }
    if let Some(base) = context.option("--base") {
        if !valid_rev(base) {
            return Err(invalid(format!(
                "--base {base_quoted} must be a commit_id: 16 lowercase hex digits",
                base_quoted = crate::error::quote(base)
            )));
        }
        body.insert("base_commit_id".into(), json!(base));
    }
    let (_, organization) = scoped_slug(context, true)?;
    let slug = reference.slug;
    let body = Value::Object(body);
    let path = format!("/programs/{}", encode_segment(&slug));
    let request = workspace::scoped(
        Request::from_manifest(context.operation, 0, path.clone()).json_body(&body),
        organization.as_deref(),
    );
    let planned = context.planned(0, &path, &[], request.body.as_deref());
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let response = context.send_raw(&request)?;
    if !(200..300).contains(&response.status) {
        let mut error = context.classify(&request, &response);
        if error.code == ErrorCode::ServiceWriteConflict {
            error = with_current(error, &response.body);
        }
        if error.code == ErrorCode::GithubLinkRequired {
            error = refine_save_forbidden(error, &response.body, context.known_credential());
        }
        if let Some(organization) = &organization {
            error = workspace::organization_failure(
                context,
                error,
                organization,
                Role::Developer,
                "saving a program",
                "--org",
                true,
            );
        }
        return Err(error);
    }
    let reply = response.json_object()?;
    let program = project(reply.get("program").unwrap_or(&Value::Null), true, false)?;
    let url = reply
        .get("url")
        .and_then(Value::as_str)
        .filter(|url| {
            url.starts_with("/p/")
                && url.len() > 3
                && url.len() <= 256
                && !url.chars().any(char::is_whitespace)
        })
        .ok_or_else(|| protocol("the service response has no valid `url`"))?;
    // The service answers with a path; the result is the absolute URL.
    let url = super::render::absolute_url(&context.environment, url)
        .ok_or_else(|| protocol("the service response has no valid `url`"))?;
    context.human = Some(format!(
        "Saved {name} rev {}: ref {name}@{} (commit {})\n",
        program["rev"],
        program["rev_id"].as_str().unwrap_or_default(),
        program["commit_id"].as_str().unwrap_or_default(),
        name = name_of(&program),
    ));
    Ok(json!({ "program": program, "url": url }))
}

/// The service's 403 texts on `PUT /programs/{slug}` (the program
/// routes and the auth layer). Only the first is a missing GitHub
/// link; the route override in the manifest names that case.
const LINK_REQUIRED_TEXT: &str = "Sharing requires a linked GitHub login";
const REJECTED_TEXTS: [&str; 2] = [
    "This handle collides with a reserved namespace",
    "Slug is owned by a different account",
];

/// Refines the manifest's `PUT /programs/{slug}` 403 override by the
/// service's `error` text: the linked-login refusal stays
/// `GITHUB_LINK_REQUIRED`; a reserved handle or a reassigned slug is
/// `SERVICE_REQUEST_REJECTED` with the service message; anything else,
/// including `Invalid API key.`, is the status default
/// `SERVICE_AUTH_REQUIRED`, so a bad key is never reported as valid.
fn refine_save_forbidden(error: RunnerError, body: &[u8], credential: Option<&str>) -> RunnerError {
    let text = serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|value| value["error"].as_str().map(str::to_owned))
        .unwrap_or_default();
    if text.starts_with(LINK_REQUIRED_TEXT) {
        return error;
    }
    if REJECTED_TEXTS.iter().any(|prefix| text.starts_with(prefix)) {
        let rejected = RunnerError::catalog(ErrorCode::ServiceRequestRejected)
            .with_detail("serviceStatus", 403);
        return match super::render::sanitize_service_message(&text, credential) {
            Some(message) => rejected.with_detail("serviceMessage", message),
            None => rejected,
        };
    }
    RunnerError::catalog(ErrorCode::ServiceAuthRequired).with_detail("serviceStatus", 403)
}

/// A stale `--base` (409 with the current record): name the current commit
/// so the caller can reconcile and retry with `--base`.
fn with_current(error: RunnerError, body: &[u8]) -> RunnerError {
    let current = serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|value| project(&value["current"], true, false).ok());
    match current {
        Some(current) => error
            .with_detail(
                "currentCommitId",
                current["commit_id"].as_str().unwrap_or_default(),
            )
            .with_detail("currentRev", current["rev"].clone()),
        None => error,
    }
}

/// VISIBILITY, case-insensitively; a typo names the nearest value.
fn visibility_argument(context: &Context<'_>) -> Result<String, RunnerError> {
    let given = context.argument("VISIBILITY").unwrap_or_default();
    let value = given.to_lowercase();
    if matches!(value.as_str(), "public" | "private") {
        return Ok(value);
    }
    let near = super::did_you_mean(&value, ["public", "private"]);
    let hint = near.map_or_else(String::new, |near| format!("; did you mean {near}?"));
    let error = invalid(format!(
        "VISIBILITY {given_quoted} must be public or private{hint}",
        given_quoted = crate::error::quote(given)
    ));
    // The did-you-mean is also the corrected command.
    Err(match near {
        Some(near) => context.corrected(
            error,
            &format!("Use {near}: `{{command}}`"),
            context.argv_with_argument(given, near),
        ),
        None => error,
    })
}

fn visibility(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    slug_reference(context)?;
    let value = visibility_argument(context)?;
    let (reference, organization) = scoped_slug(context, false)?;
    let slug = reference.slug;
    let body = json!({ "visibility": value });
    let path = format!("/programs/{}/visibility", encode_segment(&slug));
    let request = workspace::scoped(
        Request::from_manifest(context.operation, 0, path.clone()).json_body(&body),
        organization.as_deref(),
    );
    let planned = context.planned(0, &path, &[], request.body.as_deref());
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let reply = send_scoped(
        context,
        &request,
        organization.as_deref(),
        Role::Admin,
        "changing a program's visibility",
        Some(&slug),
    )
    .map_err(|error| {
        let unsaved = unsaved_program(error.clone());
        match &organization {
            Some(organization) if unsaved != error => {
                program_ref::missing_in_organization(context, organization, &slug, unsaved)
            }
            _ => unsaved,
        }
    })?;
    let program = project(reply.get("program").unwrap_or(&Value::Null), true, false)?;
    context.human = Some(format!(
        "{} is now {}\n",
        name_of(&program),
        program["visibility"].as_str().unwrap_or_default()
    ));
    Ok(json!({ "program": program }))
}

/// The service's 400 for a visibility change of a program the caller never
/// saved: it reads as a missing program (the operation's not-found
/// explanation names it and the command that lists programs).
const NEW_PROGRAM_TEXT: &str = "New program needs content.";

fn unsaved_program(error: RunnerError) -> RunnerError {
    let unsaved = error
        .details
        .as_ref()
        .and_then(|details| details.get("serviceMessage"))
        .and_then(Value::as_str)
        .is_some_and(|message| message.trim() == NEW_PROGRAM_TEXT);
    if unsaved {
        RunnerError::catalog(ErrorCode::ServiceResourceNotFound)
    } else {
        error
    }
}

fn delete(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let (reference, organization) = scoped_slug(context, false)?;
    let slug = reference.slug;
    let name = organization.as_ref().map_or_else(
        || slug.clone(),
        |organization| format!("{organization}/{slug}"),
    );
    let path = format!("/programs/{}", encode_segment(&slug));
    let planned = context.planned(0, &path, &[], None);
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let request = workspace::scoped(
        Request::from_manifest(context.operation, 0, path),
        organization.as_deref(),
    );
    let reply = match context.send(&request) {
        Ok(response) => response.json_object()?,
        Err(error) => {
            // A confirmed delete is idempotent: nothing to delete is the goal
            // state. In an organization membership was confirmed first, so a
            // plain 404 is the program; an organization 404 is not.
            let error = match &organization {
                Some(organization) => workspace::organization_failure(
                    context,
                    error,
                    organization,
                    Role::Admin,
                    "deleting a program",
                    "--org",
                    false,
                ),
                None => error,
            };
            if error.code != ErrorCode::ServiceResourceNotFound || !unexplained(&error) {
                return Err(error);
            }
            context.human = Some(format!(
                "Program {name} was already absent; nothing was deleted.\n"
            ));
            return Ok(json!({ "slug": slug, "deleted": true, "alreadyAbsent": true }));
        }
    };
    if reply.get("deleted") != Some(&Value::Bool(true)) {
        return Err(protocol("the service did not confirm the deletion"));
    }
    context.human = Some(format!("Deleted {name}\n"));
    Ok(json!({ "slug": slug, "deleted": true }))
}

fn revisions(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    // OWNER/SLUG is checked against the owner these (own) revisions name.
    let reference = slug_reference(context)?;
    let organization = workspace::organization_option(context, "--org")?;
    if let Some(organization) = &organization {
        let given = context.argument("SLUG").unwrap_or_default().to_owned();
        workspace::require_same_organization(
            context,
            &reference.owner,
            &reference.slug,
            organization,
            &given,
        )?;
    }
    let slug = reference.slug.clone();
    let path = format!("/programs/{}/revisions", encode_segment(&slug));
    let request = workspace::scoped(
        Request::from_manifest(context.operation, 0, path),
        organization.as_deref(),
    );
    // With --org the listing is the organization's, so OWNER needs no
    // confirmation.
    let owned = !reference.owner.is_empty() && organization.is_none();
    let body = match send_scoped(
        context,
        &request,
        organization.as_deref(),
        Role::Reader,
        "reading its programs",
        Some(&slug),
    ) {
        // OWNER/SLUG of a program the caller has not saved: OWNER cannot be
        // confirmed, which is not "your program is missing".
        Err(error) if owned && error.code == ErrorCode::ServiceResourceNotFound => {
            return Err(program_ref::unconfirmed_owner(context, &reference));
        }
        other => other?,
    };
    if owned
        && body
            .get("revisions")
            .and_then(Value::as_array)
            .is_some_and(Vec::is_empty)
    {
        return Err(program_ref::unconfirmed_owner(context, &reference));
    }
    match &organization {
        None => program_ref::check_owner(&reference, &body)?,
        // A service that ignores `?workspace=` answers with the caller's own
        // revisions.
        Some(organization) => {
            workspace::require_organization_owner(items(&body, "revisions")?, organization)?;
        }
    }
    let revisions = items(&body, "revisions")?
        .iter()
        .map(|record| project(record, false, false))
        .collect::<Result<Vec<_>, _>>()?;
    let mut human = String::new();
    if revisions.is_empty() {
        human.push_str("No revisions.\n");
    }
    for revision in &revisions {
        let _ = write!(
            human,
            "rev {}  rev_id={}  commit_id={}  ref={}",
            revision["rev"],
            revision["rev_id"].as_str().unwrap_or_default(),
            revision["commit_id"].as_str().unwrap_or_default(),
            revision["ref"].as_str().unwrap_or_default()
        );
        if let Some(text) = revision["message"].as_str() {
            let _ = write!(human, "  {}", human_safe_scalar(text));
        }
        human.push('\n');
    }
    context.human = Some(human);
    Ok(json!({ "revisions": revisions }))
}

/// `program copy OWNER/SLUG[@REV] --to ORG`: copies a program into an
/// organization's namespace. A bare SLUG is the caller's own personal
/// program (its owner is read from its revisions, manifest request 1). The
/// copy records its source (`copied_from`); the source is unchanged.
fn copy(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let given = context
        .argument("OWNER/SLUG[@REV]")
        .unwrap_or_default()
        .to_owned();
    let mut reference = program_ref::parse_own_allowed(&given)?;
    let to = workspace::organization_option(context, "--to")?.unwrap_or_default();
    let rename = context.option("--slug").map(str::to_owned);
    if let Some(rename) = rename.as_deref().filter(|rename| !valid_slug(rename)) {
        let lower = rename.to_ascii_lowercase();
        let error = invalid(format!(
            "--slug {} must be a program slug: lowercase letters, digits and hyphens, at most 64 characters",
            crate::error::quote(rename)
        ));
        return Err(if lower != rename && valid_slug(&lower) {
            context.corrected(
                error,
                &format!("Pass {lower}: `{{command}}`"),
                context.argv_with_option("--slug", &lower),
            )
        } else {
            error
        });
    }
    let target = rename.clone().unwrap_or_else(|| reference.slug.clone());
    // A bare SLUG the caller has not saved names `cli program list`.
    program_ref::fill_owner(context, &mut reference, 1)?;
    let path = workspace::copy_path(&reference.owner, &reference.slug, reference.rev.as_deref());
    let body = match &rename {
        Some(rename) => json!({ "to": to, "slug": rename }),
        None => json!({ "to": to }),
    };
    let request = Request::from_manifest(context.operation, 0, path.clone()).json_body(&body);
    let planned = context.planned(0, &path, &[], request.body.as_deref());
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let response = context.send_raw(&request)?;
    if !(200..300).contains(&response.status) {
        let mut error = context.classify(&request, &response);
        // A 403 without a code is the service's slug refusal (or the key
        // itself).
        if error.code == ErrorCode::ServiceAuthRequired && response.status == 403 {
            error = refine_save_forbidden(error, &response.body, context.known_credential());
        }
        let explained = workspace::organization_failure(
            context,
            error,
            &to,
            Role::Developer,
            "copying a program into it",
            "--to",
            false,
        );
        if explained.code == ErrorCode::ServiceResourceNotFound && unexplained(&explained) {
            // The service answers a missing source and a non-member the same
            // way.
            return Err(explained.with_detail(
                "reason",
                format!(
                    "program {} was not found, or you are not a member of organization {to}",
                    human_safe_scalar(&given)
                ),
            ));
        }
        return Err(explained);
    }
    let reply = response.json_object()?;
    let program = project(reply.get("program").unwrap_or(&Value::Null), true, false)?;
    if !program["owner"]
        .as_str()
        .is_some_and(|owner| owner.eq_ignore_ascii_case(&to))
        || program["slug"] != target.as_str()
    {
        return Err(protocol("the service returned a different program"));
    }
    let mut result = Map::new();
    match reply.get("url") {
        None | Some(Value::Null) => {}
        Some(url) => {
            let absolute = url
                .as_str()
                .filter(|url| {
                    url.starts_with("/p/")
                        && url.len() > 3
                        && url.len() <= 256
                        && !url.chars().any(char::is_whitespace)
                })
                .and_then(|url| super::render::absolute_url(&context.environment, url))
                .ok_or_else(|| protocol("the service response has no valid `url`"))?;
            result.insert("url".into(), json!(absolute));
        }
    }
    let from = if program["copied_from"].is_object() {
        workspace::copied_text(&program["copied_from"])
    } else {
        reference.rev.as_deref().map_or_else(
            || format!("{}/{}", reference.owner, reference.slug),
            |rev| program_ref::ref_of(&reference.owner, &reference.slug, rev),
        )
    };
    context.human = Some(format!(
        "Copied {from} to {} rev {}: ref {}; the original is unchanged\n",
        name_of(&program),
        program["rev"],
        program["ref"].as_str().unwrap_or_default()
    ));
    result.insert("program".into(), program);
    Ok(Value::Object(result))
}

/// A sentence: non-blank, at most 4096 code points, no control characters
/// other than TAB and LF.
fn valid_sentence(value: &str) -> bool {
    !value
        .chars()
        .all(|character| matches!(character, ' ' | '\t' | '\n'))
        && value.chars().count() <= MAX_SENTENCE_CHARS
        && !value.chars().any(|character| {
            (character <= '\u{1f}' && !matches!(character, '\t' | '\n')) || character == '\u{7f}'
        })
}

fn valid_model(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-')
        })
}

fn run_failed(run_id: Option<&str>, reason: impl Into<String>) -> RunnerError {
    let error = RunnerError::hosted_run_failed(reason);
    match run_id {
        Some(run_id) => error.with_detail("runId", run_id),
        None => error,
    }
}

fn valid_run_id(value: &str) -> bool {
    value.strip_prefix("run_").is_some_and(|rest| {
        (1..=128).contains(&rest.len())
            && rest
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    })
}

/// `program draft`: the hosted writer (`POST /write`) runs the platform
/// authoring program. It is read as a server-sent event stream (the service
/// sends heartbeats), so a multi-minute authoring run is bounded by the
/// stream idle timeout instead of a buffered request deadline.
fn draft(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let sentence = context.argument("SENTENCE").unwrap_or_default().to_owned();
    if !valid_sentence(&sentence) {
        return Err(invalid(format!(
            "SENTENCE must be non-blank text of at most {MAX_SENTENCE_CHARS} characters without control characters"
        )));
    }
    let mut body = Map::new();
    body.insert("request".into(), json!(sentence));
    if let Some(current) = context.option("--current") {
        let current = fs::read_text(
            &context.system.current_dir,
            current,
            MAX_PROGRAM_BYTES,
            "--current",
        )?;
        body.insert("current_program".into(), json!(current));
    }
    if let Some(model) = context.option("--model") {
        if !valid_model(model) {
            return Err(invalid(format!(
                "--model {model_quoted} is not a model id; list models with `cli model list`",
                model_quoted = crate::error::quote(model)
            )));
        }
        body.insert("model".into(), json!(model));
    }
    if let Some(target) = context.option("--output-file") {
        preflight_output(&context.system.current_dir, target)?;
    }
    if let Some(model) = body.get("model").and_then(Value::as_str).map(str::to_owned) {
        // An unknown model fails before confirmation (request 1).
        context.check_model(1, &model)?;
    }
    let request = Request::from_manifest(context.operation, 0, "/write")
        .json_body(&Value::Object(body))
        .header("Accept", "text/event-stream");
    let mut planned = context.planned(0, "/write", &[], request.body.as_deref());
    if context.invocation.preview || !context.invocation.yes {
        // A draft reserves a run's hold, quoted without options (request 2,
        // advisory).
        if let Some(quote) = context.advisory_quote(2, None)? {
            planned["quote"] = quote;
        }
    }
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let mut reader = match context.open_stream(&request)? {
        StreamOpen::Events { reader, .. } => reader,
        StreamOpen::Response(response) => return Err(context.classify(&request, &response)),
        StreamOpen::Dropped => {
            return Err(RunnerError::catalog(ErrorCode::ServiceUnavailable).with_detail(
                "reason",
                "the connection failed before the service answered; check `cli run list` before drafting again, because a draft may have started",
            ));
        }
    };
    let mut run_id: Option<String> = None;
    let mut failure: Option<String> = None;
    let complete = loop {
        let item = match reader.next_item() {
            Ok(item) => item,
            Err(error) => {
                return Err(match &run_id {
                    Some(run_id) if error.code == ErrorCode::Cancelled => error
                        .with_detail("runId", run_id.as_str())
                        .with_detail(
                            "reason",
                            format!("stopped waiting; the draft run continues on the service: `cli run show {run_id}`"),
                        ),
                    _ => error,
                });
            }
        };
        let event = match item {
            SseItem::Event(event) => event,
            SseItem::End(end) => {
                if end == StreamEnd::Closed {
                    if let Some(message) = failure {
                        return Err(run_failed(run_id.as_deref(), message));
                    }
                }
                let error = RunnerError::catalog(ErrorCode::ServiceUnavailable).with_detail(
                    "reason",
                    run_id.as_ref().map_or_else(
                        || "the draft stream ended before the run started".to_owned(),
                        |run_id| format!(
                            "the draft stream ended before the run finished; inspect it with `cli run show {run_id}`"
                        ),
                    ),
                );
                return Err(match &run_id {
                    Some(run_id) => error.with_detail("runId", run_id.as_str()),
                    None => error,
                });
            }
        };
        let data = event.json()?;
        let kind = data["type"].as_str().unwrap_or(event.event.as_str());
        if let Some(id) = data["run_id"].as_str().filter(|id| valid_run_id(id)) {
            run_id.get_or_insert_with(|| id.to_owned());
        }
        match kind {
            "run_complete" => break data,
            "error" => {
                failure = Some(
                    data["message"]
                        .as_str()
                        .and_then(|message| {
                            super::render::sanitize_service_message(
                                message,
                                context.known_credential(),
                            )
                        })
                        .map_or_else(
                            || "the hosted writer reported an error".to_owned(),
                            |message| {
                                format!(
                                    "the hosted writer reported an error: {}",
                                    super::render::run_error_text(&message)
                                )
                            },
                        ),
                );
            }
            _ => {}
        }
    };
    let status = complete["status"].as_str().unwrap_or_default();
    if status != "completed" {
        let reason = failure.unwrap_or_else(|| {
            format!(
                "the hosted writer run ended with status {}",
                crate::error::quote(&human_safe_scalar(status))
            )
        });
        return Err(run_failed(run_id.as_deref(), reason));
    }
    let response = decode_draft(complete["response"].as_str().unwrap_or_default());
    let trimmed = response.trim_matches(|c| matches!(c, ' ' | '\t' | '\n' | '\r'));
    if trimmed.is_empty() {
        return Err(run_failed(
            run_id.as_deref(),
            "the hosted writer returned no program",
        ));
    }
    let bytes = format!("{trimmed}\n").into_bytes();
    let file = written_file(context, &bytes)?;
    context.human = Some(match file["content"].as_str() {
        Some(text) => text.to_owned(),
        None => file_line("The draft", &file, context.option("--output-file")),
    });
    let mut result = Map::new();
    result.insert("file".into(), file);
    if let Some(run_id) = &run_id {
        result.insert("runId".into(), json!(run_id));
    }
    // Server-provided billing, copied verbatim (never computed here).
    for key in ["price_cents", "environment_price_cents"] {
        if let Some(value) = complete[key]
            .as_i64()
            .filter(|value| value.unsigned_abs() <= MAX_SAFE_INTEGER)
        {
            result.insert(key.into(), json!(value));
        }
    }
    if let Some(status) = complete["billing_status"]
        .as_str()
        .filter(|status| super::render::valid_text(status, 32))
    {
        result.insert(
            "billing_status".into(),
            json!(super::render::public_billing(status)),
        );
    }
    Ok(Value::Object(result))
}

/// The hosted writer's `run_complete.response` is the raw bytes of the run's
/// `outputs/result.json`: prose-write returns ONE string, so the program
/// arrives as a JSON string literal. The service unwraps it only on its
/// blocking JSON path, so the stream reader decodes it the same way here:
/// a JSON string is unwrapped, and an escaped one-liner (literal `\n`
/// sequences with no real newline, often with a stray wrapping quote) is
/// decoded with the service's backstop. Anything else is kept as is.
fn decode_draft(response: &str) -> String {
    if let Ok(Value::String(text)) = serde_json::from_str::<Value>(response) {
        return text;
    }
    if !response.contains('\n') && response.contains("\\n") {
        let mut text = response.trim_matches(|c| matches!(c, ' ' | '\t' | '\n' | '\r'));
        text = text.strip_prefix('"').unwrap_or(text);
        text = text.strip_suffix('"').unwrap_or(text);
        return text
            .replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\"", "\"")
            .replace("\\\\", "\\");
    }
    response.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_admits_tab_lf_cr_only() {
        assert!(is_text("a\tb\r\nc"));
        assert!(is_text(""));
        assert!(!is_text("a\u{1b}[31m"));
        assert!(!is_text("a\u{7f}"));
    }

    #[test]
    fn projection_is_strict_and_drops_content() {
        let record = json!({
            "owner": "alice", "slug": "demo", "visibility": "private", "content": "x\n",
            "rev": 2, "rev_id": "0123456789abcdef", "commit_id": "fedcba9876543210",
            "parent_commit_id": null, "updated_at": 1, "message": "a\nb", "ownerCustomerId": "cus_1"
        });
        let projected = project(&record, true, false).unwrap();
        assert_eq!(projected["message"], "a b");
        assert!(projected.get("content").is_none());
        assert!(projected.get("ownerCustomerId").is_none());
        assert_eq!(project(&record, true, true).unwrap()["content"], "x\n");
        let mut bad = record.clone();
        bad["rev"] = json!(0);
        assert!(project(&bad, true, false).is_err());
        let mut bad = record;
        bad["rev_id"] = json!("XYZ");
        assert!(project(&bad, true, false).is_err());
    }

    #[test]
    fn draft_response_is_decoded_like_the_service() {
        // Service shape: outputs/result.json holds one JSON string.
        assert_eq!(
            decode_draft(r#""---\nname: hi\nkind: function\n---\n""#),
            "---\nname: hi\nkind: function\n---\n"
        );
        // Escaped one-liner with a stray wrapping quote (JSON.parse fails).
        assert_eq!(
            decode_draft(r#""---\nname: \"q\"\ta\\b\r\nx"#),
            "---\nname: \"q\"\ta\\b\nx"
        );
        // Plain multi-line text and non-string JSON are kept as is.
        assert_eq!(decode_draft("---\nname: hi\n"), "---\nname: hi\n");
        assert_eq!(decode_draft(r#"{"text":"x"}"#), r#"{"text":"x"}"#);
    }

    #[test]
    fn save_forbidden_is_refined_by_service_text() {
        let base = || {
            RunnerError::catalog(ErrorCode::GithubLinkRequired).with_detail("serviceStatus", 403)
        };
        let code = |body: &str| refine_save_forbidden(base(), body.as_bytes(), None).code;
        assert_eq!(
            code(
                r#"{"error":"Sharing requires a linked GitHub login (it becomes your public handle). Sign in via GitHub first."}"#
            ),
            ErrorCode::GithubLinkRequired
        );
        assert_eq!(
            code(r#"{"error":"Invalid API key."}"#),
            ErrorCode::ServiceAuthRequired
        );
        assert_eq!(code("not json"), ErrorCode::ServiceAuthRequired);
        assert_eq!(
            code(r#"{"error":"Slug is owned by a different account."}"#),
            ErrorCode::ServiceRequestRejected
        );
    }

    #[test]
    fn sentences_and_models() {
        assert!(valid_sentence("write a haiku\nabout rain"));
        assert!(!valid_sentence("  "));
        assert!(!valid_sentence("bell\u{7}"));
        assert!(valid_model("model-luna"));
        assert!(!valid_model("GPT"));
    }
}
