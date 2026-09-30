//! Published results: `result list|show` (anonymous reads of a
//! public program's publications) and `result publish|unpublish` (owner-only,
//! confirm-class).
//!
//! - The reads send no `Authorization` header: their manifest auth class is
//!   `none`, so they work without a key.
//! - `result show --raw` first reads the publication detail (by id or
//!   `--latest`) and then fetches the raw bytes of exactly that publication, so
//!   the reported publication and the bytes always match even when a newer
//!   result is published in between.
//! - `--output-file` streams the raw bytes to a new file under the download
//!   limits; the file is created before any request and removed on failure.
//!   Without it the bytes (at most the control limit) are returned inline as
//!   `raw.content` when they are text.
//! - Responses are projected onto the closed `service/results.schema.json`
//!   shapes (`raw_url` and `file_urls` are dropped); anything else is
//!   `SERVICE_PROTOCOL_INVALID` with a `reason` naming the field.
//!
//! Mirrors `cli/bun/src/core/service/results.ts` byte for byte.
use super::fs::{create_new_file, valid_relative_path};
use super::http::{Request, TransportClass, encode_segment};
use super::program_ref::{self, ProgramRef, valid_owner, valid_rev, valid_slug};
use super::render::{dollars_or_dash, valid_text};
use super::{Context, Environment, Gate};
use crate::error::{ErrorCode, human_safe_scalar};
use crate::{OutputMode, RunnerError};
use serde_json::{Map, Value, json};
use std::fmt::Write as _;
use std::io::Write;

/// Request indexes in the manifest (`operations.v1.json`).
const SHOW_BY_ID: usize = 0;
const SHOW_LATEST: usize = 1;
const SHOW_RAW: usize = 2;
/// `GET /programs/{slug}/revisions`: the owner of a bare SLUG (list 1, show
/// 4) or the OWNER/SLUG check (publish, unpublish 1).
const LIST_REVISIONS: usize = 1;
const SHOW_REVISIONS: usize = 4;
const OWNER_CHECK: usize = 1;
/// Most publications a list response may carry (`resultList.results.maxItems`).
const MAX_LIST_ITEMS: usize = 200;
/// Most files a published manifest may name (`resultManifest.files.maxItems`).
const MAX_MANIFEST_FILES: usize = 16;
/// Publication ids and `spec_commit` (code points).
const MAX_ID: usize = 64;
/// `contentType` (code points).
const MAX_CONTENT_TYPE: usize = 128;
/// `--limit` bounds (manifest option description).
const LIMIT_RANGE: std::ops::RangeInclusive<u64> = 1..=100;

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

/// `SERVICE_PROTOCOL_INVALID` naming the offending response field.
fn malformed(field: &str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
        .with_detail("reason", format!("the service returned an invalid {field}"))
}

/// Adds a corrective `reason` to `SERVICE_RESOURCE_NOT_FOUND`.
fn explain_not_found(error: RunnerError, reason: impl FnOnce() -> String) -> RunnerError {
    if error.code == ErrorCode::ServiceResourceNotFound {
        error.with_detail("reason", reason())
    } else {
        error
    }
}

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.operation["id"].as_str().unwrap_or_default() {
        "result.list" => list(context),
        "result.show" => show(context),
        "result.publish" => publish(context),
        "result.unpublish" => unpublish(context),
        _ => context.not_implemented(),
    }
}

// ---------------------------------------------------------------- arguments

/// `OWNER/SLUG` of a public program, or a bare SLUG for the caller's own
/// program (owner "" until `owner_of` reads it). A pinned
/// `@REV` is refused: results belong to the program, not to one revision.
fn public_program(value: &str) -> Result<(String, String), RunnerError> {
    let split = |name: &str| {
        name.split_once('/')
            .filter(|(owner, slug)| valid_owner(owner) && valid_slug(slug))
            .map(|(owner, slug)| (owner.to_owned(), slug.to_owned()))
    };
    if let Some((name, _)) = value.split_once('@') {
        if split(name).is_some() || valid_slug(name) {
            return Err(invalid(format!(
                "results belong to a program, not a revision; pass {name_quoted} without @REV",
                name_quoted = crate::error::quote(name)
            )));
        }
    }
    if valid_slug(value) {
        return Ok((String::new(), value.to_owned()));
    }
    split(value).ok_or_else(|| {
        // `cli result` reads published results, not run records.
        let hint = if value.starts_with("run_") {
            format!(
                "{value_quoted} is a run id, and `cli result` reads the published results of a public program; read a run's output with `cli run show {value}` or `cli run download {value} --output-dir DIR`", value_quoted = crate::error::quote(value))
        } else {
            format!(
                "program {value_quoted} must be OWNER/SLUG (an owner handle and a lowercase program slug) or a bare SLUG for your own program; `cli result` reads published results, and a run's own output is `cli run show RUN_ID`", value_quoted = crate::error::quote(value))
        };
        invalid(hint)
    })
}

/// The owner of a bare SLUG from the caller's own revisions (manifest
/// request `index`).
fn owner_of(
    context: &mut Context<'_>,
    owner: String,
    slug: &str,
    index: usize,
) -> Result<String, RunnerError> {
    if !owner.is_empty() {
        return Ok(owner);
    }
    let mut reference = ProgramRef {
        owner,
        slug: slug.to_owned(),
        rev: None,
        rev_number: None,
    };
    program_ref::fill_owner(context, &mut reference, index)?;
    Ok(reference.owner)
}

/// `OWNER/SLUG` for a hint: the confirmed owner, or the bare own slug that
/// `result list` and `run submit --from` resolve.
fn program_name(owner: &str, slug: &str) -> String {
    if owner.is_empty() {
        slug.to_owned()
    } else {
        format!("{owner}/{slug}")
    }
}

/// A copyable follow-up command line that keeps the machine output mode.
/// The shared renderer is `render::follow_up_command`.
fn command(hint: (&Environment, OutputMode), words: &str) -> String {
    super::render::follow_up_command(hint.1, words)
}

/// Publication ids are opaque; the CLI admits only URL-safe tokens.
fn valid_publication_id(value: &str) -> bool {
    (1..=MAX_ID).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn publication_id(
    hint: (&Environment, OutputMode),
    value: &str,
    program: &str,
) -> Result<String, RunnerError> {
    if valid_publication_id(value) {
        Ok(value.to_owned())
    } else {
        Err(invalid(format!(
            "<PUBLICATION_ID> {value_quoted} must be a publication id as printed by `{}` (letters, digits, - and _)",
            command(hint, &format!("result list {program}")),
            value_quoted = crate::error::quote(value)
        )))
    }
}

/// `^run_[A-Za-z0-9_-]{1,128}$`.
fn valid_run_id(value: &str) -> bool {
    value.strip_prefix("run_").is_some_and(|rest| {
        (1..=128).contains(&rest.len())
            && rest
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    })
}

fn option_default(context: &Context<'_>, name: &str) -> Option<&'static str> {
    context.operation["options"]
        .as_array()?
        .iter()
        .find(|option| option["name"] == name)?["default"]
        .as_str()
}

fn limit(context: &Context<'_>) -> Result<u64, RunnerError> {
    let value = context
        .option("--limit")
        .or_else(|| option_default(context, "--limit"))
        .unwrap_or("20");
    value
        .parse::<u64>()
        .ok()
        .filter(|number| {
            !value.is_empty()
                && value.len() <= 3
                && value.bytes().all(|byte| byte.is_ascii_digit())
                && LIMIT_RANGE.contains(number)
        })
        .ok_or_else(|| {
            context.limit_error(
                invalid(format!(
                    "--limit must be an integer from 1 to 100, got {value_quoted}",
                    value_quoted = crate::error::quote(value)
                )),
                value,
                *LIMIT_RANGE.end(),
            )
        })
}

fn results_path(owner: &str, slug: &str) -> String {
    format!(
        "/p/{}/{}/results",
        encode_segment(owner),
        encode_segment(slug)
    )
}

// ---------------------------------------------------------------- projection

fn valid_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    (13..=40).contains(&bytes.len())
        && bytes[..10].iter().enumerate().all(|(index, byte)| {
            if index == 4 || index == 7 {
                *byte == b'-'
            } else {
                byte.is_ascii_digit()
            }
        })
        && bytes[10] == b'T'
        && bytes[bytes.len() - 1] == b'Z'
        && bytes[11..bytes.len() - 1]
            .iter()
            .all(|byte| byte.is_ascii_digit() || *byte == b':' || *byte == b'.')
}

fn valid_model(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'.' || *byte == b'-'
        })
}

fn valid_program_ref(value: &str) -> bool {
    value.len() <= 200
        && value.split_once('@').is_some_and(|(name, rev)| {
            valid_rev(rev)
                && name
                    .split_once('/')
                    .is_some_and(|(owner, slug)| valid_owner(owner) && valid_slug(slug))
        })
}

fn string_field<'a>(
    object: &'a Map<String, Value>,
    key: &str,
    path: &str,
    valid: impl Fn(&str) -> bool,
) -> Result<&'a str, RunnerError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| valid(value))
        .ok_or_else(|| malformed(&format!("{path}.{key}")))
}

fn non_negative_integer(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64)
}

/// One publication summary, projected onto `results.schema.json#/$defs/publication`.
fn publication(value: Option<&Value>, path: &str) -> Result<Value, RunnerError> {
    let object = value
        .and_then(Value::as_object)
        .ok_or_else(|| malformed(path))?;
    let id = string_field(object, "id", path, |value| valid_text(value, MAX_ID))?;
    let published_at = string_field(object, "published_at", path, valid_timestamp)?;
    let run_id = string_field(object, "run_id", path, valid_run_id)?;
    let program_ref = string_field(object, "program_ref", path, valid_program_ref)?;
    let created_at = string_field(object, "created_at", path, valid_timestamp)?;
    let model = string_field(object, "model", path, valid_model)?;
    string_field(object, "status", path, |value| value == "completed")?;
    Ok(json!({
        "id": id,
        "published_at": published_at,
        "run_id": run_id,
        "program_ref": program_ref,
        "created_at": created_at,
        "model": model,
        "status": "completed",
    }))
}

/// The public manifest, projected onto `resultManifest` (`file_urls` dropped).
fn manifest(value: Option<&Value>) -> Result<Value, RunnerError> {
    let path = "manifest";
    let object = value
        .and_then(Value::as_object)
        .ok_or_else(|| malformed(path))?;
    let run_id = string_field(object, "run_id", path, valid_run_id)?;
    string_field(object, "status", path, |value| value == "completed")?;
    let model = string_field(object, "model", path, valid_model)?;
    let program_ref = string_field(object, "program_ref", path, valid_program_ref)?;
    let created_at = string_field(object, "created_at", path, valid_timestamp)?;
    let usage = object
        .get("usage")
        .and_then(Value::as_object)
        .and_then(|usage| {
            Some(json!({
                "input_tokens": non_negative_integer(usage.get("input_tokens"))?,
                "output_tokens": non_negative_integer(usage.get("output_tokens"))?,
            }))
        })
        .ok_or_else(|| malformed("manifest.usage"))?;
    let price_cents = object
        .get("price_cents")
        .and_then(Value::as_i64)
        .ok_or_else(|| malformed("manifest.price_cents"))?;
    let files = object
        .get("files")
        .and_then(Value::as_array)
        .filter(|files| files.len() <= MAX_MANIFEST_FILES)
        .and_then(|files| {
            files
                .iter()
                .map(|file| {
                    file.as_str()
                        .filter(|file| valid_relative_path(file))
                        .map(|file| Value::String(file.to_owned()))
                })
                .collect::<Option<Vec<_>>>()
        })
        .ok_or_else(|| malformed("manifest.files"))?;
    let has_patch = object
        .get("has_patch")
        .and_then(Value::as_bool)
        .ok_or_else(|| malformed("manifest.has_patch"))?;
    let mut projected = json!({
        "run_id": run_id,
        "status": "completed",
        "model": model,
        "program_ref": program_ref,
        "created_at": created_at,
        "usage": usage,
        "price_cents": price_cents,
        "files": files,
        "has_patch": has_patch,
    });
    match object.get("spec_commit") {
        None | Some(Value::Null) => {}
        Some(Value::String(commit)) if valid_text(commit, MAX_ID) => {
            projected["spec_commit"] = Value::String(commit.clone());
        }
        Some(_) => return Err(malformed("manifest.spec_commit")),
    }
    Ok(projected)
}

// ---------------------------------------------------------------- operations

fn list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let (given, slug) = public_program(context.argument("OWNER/SLUG").unwrap_or_default())?;
    let limit = limit(context)?;
    let owner = owner_of(context, given, &slug, LIST_REVISIONS)?;
    let request = Request::from_manifest(context.operation, 0, results_path(&owner, &slug))
        .query("limit", limit.to_string());
    let body = context
        .send(&request)
        .map_err(|error| {
            explain_not_found(error, || {
                format!("{owner}/{slug} is not a public program (it does not exist or is private)")
            })
        })?
        .json_object()?;
    let contract = body
        .get("contract")
        .and_then(Value::as_object)
        .ok_or_else(|| malformed("contract"))?;
    let contract = json!({
        "owner": string_field(contract, "owner", "contract", valid_owner)?,
        "slug": string_field(contract, "slug", "contract", valid_slug)?,
        "rev_id": string_field(contract, "rev_id", "contract", valid_rev)?,
    });
    let items = body
        .get("results")
        .and_then(Value::as_array)
        .filter(|items| items.len() <= MAX_LIST_ITEMS)
        .ok_or_else(|| malformed("results"))?;
    let results = items
        .iter()
        .enumerate()
        .map(|(index, item)| publication(Some(item), &format!("results[{index}]")))
        .collect::<Result<Vec<_>, _>>()?;
    context.human = Some(human_list(&contract, &results));
    Ok(json!({"contract": contract, "results": results}))
}

/// The local output file of `result show --raw --output-file`; removed again
/// unless the command succeeds.
struct OutputFile {
    path: std::path::PathBuf,
    file: Option<std::fs::File>,
}

impl Drop for OutputFile {
    fn drop(&mut self) {
        if self.file.is_some() {
            self.file = None;
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

fn show(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let (given_owner, slug) = public_program(context.argument("OWNER/SLUG").unwrap_or_default())?;
    let latest = context.flag("--latest");
    let raw = context.flag("--raw");
    // No id, `latest` or --latest all read the newest publication.
    let requested = match (context.argument("PUBLICATION_ID"), latest) {
        (None | Some("latest"), _) => None,
        (Some(_), true) => {
            return Err(invalid(
                "give either <PUBLICATION_ID> or --latest, not both",
            ));
        }
        (Some(id), false) => Some(publication_id(
            (&context.environment, context.mode),
            id,
            &program_name(&given_owner, &slug),
        )?),
    };
    let output_file = context.option("--output-file").map(str::to_owned);
    if output_file.is_some() && !raw {
        return Err(invalid(
            "--output-file writes the raw result bytes; add --raw",
        ));
    }
    let owner = owner_of(context, given_owner, &slug, SHOW_REVISIONS)?;
    let mut output = match &output_file {
        Some(value) => {
            let file = create_new_file(&context.system.current_dir, value)?;
            let path = if std::path::Path::new(value).is_absolute() {
                std::path::PathBuf::from(value)
            } else {
                context.system.current_dir.join(value)
            };
            Some(OutputFile {
                path,
                file: Some(file),
            })
        }
        None => None,
    };

    let base = results_path(&owner, &slug);
    let (index, path) = match &requested {
        Some(id) => (SHOW_BY_ID, format!("{base}/{}", encode_segment(id))),
        None => (SHOW_LATEST, format!("{base}/latest")),
    };
    let list_command = command(
        (&context.environment, context.mode),
        &format!("result list {owner}/{slug}"),
    );
    let body = context
        .send(&Request::from_manifest(context.operation, index, path))
        .map_err(|error| {
            explain_not_found(error, || match &requested {
                Some(id) => format!(
                    "no publication {id} of {owner}/{slug}; list them with `{list_command}`"
                ),
                None => format!(
                    "{owner}/{slug} has no published results or is not a public program; check with `{list_command}`"
                ),
            })
        })?
        .json_object()?;
    let publication = publication(body.get("publication"), "publication")?;
    let id = publication["id"].as_str().unwrap_or_default().to_owned();
    if requested.as_ref().is_some_and(|requested| *requested != id) {
        return Err(malformed("publication.id"));
    }
    if !raw {
        let manifest = manifest(body.get("manifest"))?;
        context.human = Some(human_show(&publication, Some(&manifest)));
        return Ok(json!({"publication": publication, "manifest": manifest}));
    }
    if !valid_publication_id(&id) {
        return Err(malformed("publication.id"));
    }

    let request = Request::from_manifest(
        context.operation,
        SHOW_RAW,
        format!("{base}/{}/raw", encode_segment(&id)),
    )
    .class(TransportClass::Download);
    let mut written = json!({"written": output.is_some()});
    if let Some(output) = output.as_mut() {
        let file = output.file.as_mut().expect("output file is open");
        let limit = download_limit();
        let downloaded = context.download(&request, file, limit)?;
        file.flush()
            .and_then(|()| file.sync_all())
            .map_err(|_| invalid("cannot write the output file"))?;
        written["bytes"] = json!(downloaded.bytes);
        written["sha256"] = json!(downloaded.sha256);
        if let Some(content_type) = content_type(&downloaded.headers) {
            written["contentType"] = json!(content_type);
        }
        output.file = None; // keep the file
        context.human = Some(human_written(&publication, &written));
    } else {
        let limit = TransportClass::Control.max_response_bytes();
        let mut bytes = Vec::new();
        let downloaded = context
            .download(&request, &mut bytes, limit)
            .map_err(|error| {
                if error.code == ErrorCode::ServiceResponseTooLarge {
                    error.with_detail(
                        "reason",
                        format!(
                            "the raw result is larger than {limit} bytes; rerun with --output-file FILE"
                        ),
                    )
                } else {
                    error
                }
            })?;
        written["bytes"] = json!(downloaded.bytes);
        written["sha256"] = json!(downloaded.sha256);
        if let Some(content_type) = content_type(&downloaded.headers) {
            written["contentType"] = json!(content_type);
        }
        let text = String::from_utf8(bytes).ok().filter(|text| raw_text(text));
        if let Some(text) = &text {
            written["content"] = json!(text);
        }
        context.human = Some(match &text {
            Some(text) => text.clone(),
            None => human_written(&publication, &written),
        });
    }
    Ok(json!({"publication": publication, "raw": written}))
}

/// The download class's per-file cap.
fn download_limit() -> u64 {
    super::manifest()["transportClasses"]["download"]["maxFileBytes"]
        .as_u64()
        .unwrap_or(64 << 20)
}

/// The `common.schema.json#/$defs/text` predicate: no C0 control except TAB,
/// LF and CR, no DEL, at most 1 MiB code points.
fn raw_text(text: &str) -> bool {
    text.chars().count() <= 1_048_576
        && !text.chars().any(|character| {
            (character <= '\u{1f}' && !matches!(character, '\t' | '\n' | '\r'))
                || character == '\u{7f}'
        })
}

fn content_type(headers: &std::collections::BTreeMap<String, String>) -> Option<String> {
    headers
        .get("content-type")
        .filter(|value| valid_text(value, MAX_CONTENT_TYPE))
        .cloned()
}

fn publish(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let reference = program_ref::own_slug_argument(context)?;
    let run_id = context.option("--run").unwrap_or_default().to_owned();
    if !valid_run_id(&run_id) {
        return Err(invalid(format!(
            "--run {run_id_quoted} must be a run id (run_…); find completed runs with `{}`",
            command((&context.environment, context.mode), "run list"),
            run_id_quoted = crate::error::quote(&run_id)
        )));
    }
    let owner = program_ref::confirm_own_slug(context, &reference, OWNER_CHECK, false)?;
    let slug = reference.slug;
    let path = format!("/programs/{}/results", encode_segment(&slug));
    let request = Request::from_manifest(context.operation, 0, path.clone())
        .json_body(&json!({"run_id": run_id}));
    let planned = context.planned(0, &path, &[], request.body.as_deref());
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let response = context.send_raw(&request)?;
    if !(200..300).contains(&response.status) {
        let error = context.classify(&request, &response);
        return Err(explain_publish_failure(
            error,
            &response,
            (&context.environment, context.mode),
            &owner,
            &slug,
            &run_id,
        ));
    }
    let body = response.json_object()?;
    let publication = publication(body.get("publication"), "publication")?;
    context.human = Some(human_published(&context.environment, &publication));
    Ok(json!({"publication": publication}))
}

/// Adds a corrective `reason` for the publish refusals the service reports by
/// body code (they classify by status alone).
fn explain_publish_failure(
    error: RunnerError,
    response: &super::http::Response,
    hint: (&Environment, OutputMode),
    owner: &str,
    slug: &str,
    run_id: &str,
) -> RunnerError {
    let code = response
        .json_object()
        .ok()
        .and_then(|body| body.get("code").and_then(Value::as_str).map(str::to_owned));
    let reason = match (response.status, code.as_deref()) {
        // Never hand out a --yes here: making a program public exposes its
        // source to everyone, a larger decision than the publish the caller
        // asked for, so the hint is the preview and names the exposure.
        (409, Some("program_not_public")) => format!(
            "program {slug_quoted} is private and only public programs can publish results; making it public exposes its source to everyone and is the owner's decision; review that change with `{}`",
            command(hint, &format!("program visibility {slug} public --preview")),
            slug_quoted = crate::error::quote(slug)
        ),
        (409, Some("run_not_completed")) => {
            format!("run {run_id} did not complete; only completed runs can be published")
        }
        (409, Some("program_ref_mismatch")) => format!(
            "run {run_id} was not run from a saved revision of program {slug_quoted}; publish a run started with `{}`",
            command(
                hint,
                &format!("run submit --from {}", program_name(owner, slug))
            ),
            slug_quoted = crate::error::quote(slug)
        ),
        (409, Some("canonical_result_missing")) => {
            format!("run {run_id} has no outputs/result.json to publish")
        }
        (404, Some("run_not_found")) => {
            let reason = format!(
                "no run {run_id} in this account; find completed runs with `{}`",
                command(hint, "run list")
            );
            return super::not_found::explain_not_found(
                error.with_detail("reason", reason),
                hint.1,
                "run",
                run_id,
                Some(&["run", "list"]),
            );
        }
        (404, _) => format!(
            "no program {slug_quoted} owned by this account; list yours with `{}`",
            command(hint, "program list"),
            slug_quoted = crate::error::quote(slug)
        ),
        _ => return error,
    };
    error.with_detail("reason", reason)
}

fn unpublish(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let reference = program_ref::own_slug_argument(context)?;
    let id = publication_id(
        (&context.environment, context.mode),
        context.argument("PUBLICATION_ID").unwrap_or_default(),
        &program_name(&reference.owner, &reference.slug),
    )?;
    let owner = program_ref::confirm_own_slug(context, &reference, OWNER_CHECK, false)?;
    let slug = reference.slug;
    let path = format!(
        "/programs/{}/results/{}",
        encode_segment(&slug),
        encode_segment(&id)
    );
    let planned = context.planned(0, &path, &[], None);
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let request = Request::from_manifest(context.operation, 0, path);
    let list_command = command(
        (&context.environment, context.mode),
        &format!("result list {}", program_name(&owner, &slug)),
    );
    let body = context
        .send(&request)
        .map_err(|error| {
            explain_not_found(error, || {
                format!(
                    "no publication {id} on your program {slug_quoted}; list them with `{list_command}`", slug_quoted = crate::error::quote(&slug))
            })
        })?
        .json_object()?;
    if body.get("unpublished") != Some(&Value::Bool(true)) {
        return Err(malformed("unpublished"));
    }
    let returned = body
        .get("id")
        .and_then(Value::as_str)
        .filter(|value| valid_text(value, MAX_ID))
        .ok_or_else(|| malformed("id"))?
        .to_owned();
    context.human = Some(format!("Unpublished {}\n", human_safe_scalar(&returned)));
    Ok(json!({"unpublished": true, "id": returned}))
}

// ---------------------------------------------------------------- human text

fn text(value: &Value) -> String {
    human_safe_scalar(value.as_str().unwrap_or_default())
}

fn human_list(contract: &Value, results: &[Value]) -> String {
    let reference = format!(
        "{}/{}@{}",
        text(&contract["owner"]),
        text(&contract["slug"]),
        text(&contract["rev_id"])
    );
    let mut out = format!("{reference}: ");
    match results.len() {
        0 => return format!("No published results for {reference}.\n"),
        1 => out.push_str("1 published result\n"),
        count => {
            let _ = writeln!(out, "{count} published results");
        }
    }
    for item in results {
        let _ = writeln!(
            out,
            "{}  {}  {}  {}",
            text(&item["id"]),
            text(&item["published_at"]),
            text(&item["run_id"]),
            text(&item["model"])
        );
    }
    out
}

fn human_publication(publication: &Value) -> String {
    format!(
        "Publication {} of {}\nPublished: {}\nRun: {} ({}, {})\n",
        text(&publication["id"]),
        text(&publication["program_ref"]),
        text(&publication["published_at"]),
        text(&publication["run_id"]),
        text(&publication["model"]),
        text(&publication["created_at"])
    )
}

fn human_show(publication: &Value, manifest: Option<&Value>) -> String {
    let mut out = human_publication(publication);
    if let Some(manifest) = manifest {
        let files = manifest["files"]
            .as_array()
            .map(|files| files.iter().map(text).collect::<Vec<_>>().join(", "))
            .unwrap_or_default();
        let _ = writeln!(out, "Price: {}", dollars_or_dash(&manifest["price_cents"]));
        let _ = writeln!(
            out,
            "Tokens: {} in, {} out",
            manifest["usage"]["input_tokens"], manifest["usage"]["output_tokens"]
        );
        let _ = writeln!(out, "Files: {files}");
    }
    out
}

fn human_written(publication: &Value, written: &Value) -> String {
    let mut out = human_publication(publication);
    let _ = writeln!(
        out,
        "Raw result: {} bytes, sha256 {}",
        written["bytes"],
        text(&written["sha256"])
    );
    if written["written"] == true {
        out.push_str("Wrote the raw result to the output file.\n");
    } else {
        out.push_str("The raw result is not text; rerun with --output-file FILE to save it.\n");
    }
    out
}

fn human_published(environment: &Environment, publication: &Value) -> String {
    let program = publication["program_ref"]
        .as_str()
        .and_then(|reference| reference.split_once('@'))
        .map_or_else(String::new, |(name, _)| human_safe_scalar(name));
    format!(
        "Published {} for {} on {}\nPublic: {}\n",
        text(&publication["id"]),
        text(&publication["run_id"]),
        text(&publication["program_ref"]),
        command(
            (environment, OutputMode::Human),
            &format!("result show {program} {}", text(&publication["id"]))
        )
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn publication_fixture() -> Value {
        json!({
            "id": "AbCdEf012_-x",
            "published_at": "2026-09-23T20:30:00.000Z",
            "run_id": "run_abc",
            "program_ref": "Owner/demo@0123456789abcdef",
            "created_at": "2026-09-23T20:26:28.686Z",
            "model": "model-sol",
            "status": "completed",
            "extra": "dropped"
        })
    }

    #[test]
    fn public_program_arguments_reject_revisions_and_bad_names() {
        assert_eq!(
            public_program("Owner/demo").unwrap(),
            ("Owner".to_owned(), "demo".to_owned())
        );
        let error = public_program("Owner/demo@0123456789abcdef").unwrap_err();
        assert!(
            error.details.unwrap()["reason"]
                .as_str()
                .unwrap()
                .contains("without @REV")
        );
        // A bare SLUG is the caller's own program.
        assert_eq!(
            public_program("demo").unwrap(),
            (String::new(), "demo".to_owned())
        );
        assert!(public_program("demo@0123456789abcdef").is_err());
        for bad in ["Demo", "Owner/Demo", "-x/demo", "a/b/c", ""] {
            assert!(public_program(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn identifiers_follow_the_schema_predicates() {
        assert!(valid_run_id("run_abc-_9"));
        assert!(!valid_run_id("run_") && !valid_run_id("abc") && !valid_run_id("run_a/b"));
        assert!(valid_publication_id("AbCdEf012_-x"));
        assert!(!valid_publication_id("a/b") && !valid_publication_id(""));
        assert!(valid_timestamp("2026-09-23T20:26:28.686Z"));
        assert!(!valid_timestamp("2026-09-23 20:26:28Z") && !valid_timestamp("2026-09-23TZ"));
        assert!(valid_program_ref("Owner/demo@0123456789abcdef"));
        assert!(!valid_program_ref("Owner/demo"));
    }

    #[test]
    fn publications_are_projected_onto_the_closed_shape() {
        let value = publication(Some(&publication_fixture()), "publication").unwrap();
        assert!(value.get("extra").is_none());
        assert_eq!(value.as_object().unwrap().len(), 7);
        let mut bad = publication_fixture();
        bad["status"] = json!("error");
        let error = publication(Some(&bad), "results[3]").unwrap_err();
        assert_eq!(
            error.details.unwrap()["reason"],
            "the service returned an invalid results[3].status"
        );
    }

    #[test]
    fn manifests_drop_signed_urls_and_keep_spec_commit() {
        let value = manifest(Some(&json!({
            "run_id": "run_abc",
            "status": "completed",
            "model": "model-sol",
            "program_ref": "Owner/demo@0123456789abcdef",
            "created_at": "2026-09-23T20:26:28.686Z",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "price_cents": 4,
            "files": ["outputs/result.json"],
            "has_patch": false,
            "spec_commit": "abc",
            "file_urls": {"outputs/result.json": "https://example.invalid/?tok=secret"}
        })))
        .unwrap();
        assert!(value.get("file_urls").is_none());
        assert_eq!(value["spec_commit"], "abc");
        assert!(manifest(Some(&json!({"run_id": "run_abc"}))).is_err());
    }

    #[test]
    fn raw_text_matches_the_common_text_predicate() {
        assert!(raw_text("{\"a\":1}\n\t\r"));
        assert!(!raw_text("\u{1b}[31m") && !raw_text("\u{7f}"));
    }

    /// Copyable hints name no service and keep the
    /// output mode.
    #[test]
    fn follow_up_commands_name_no_service() {
        let environment = Environment::production();
        let human = human_published(&environment, &publication_fixture());
        assert!(human.contains("Public: prose cli result show Owner/demo AbCdEf012_-x\n"));
        assert_eq!(
            command((&environment, OutputMode::Human), "run list"),
            "prose cli run list"
        );
        assert_eq!(
            command((&environment, OutputMode::Json), "run list"),
            "prose --output json cli run list"
        );
    }
}
