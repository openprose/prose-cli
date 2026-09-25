//! Service run records: `cli run list|show|download|share`.
//!
//! Projections keep the service's field names verbatim, drop unknown fields
//! and never output `file_urls` (signed `tok=` URLs) or `customer_id`. A
//! known field with the wrong shape is `SERVICE_PROTOCOL_INVALID`, so service
//! drift is loud. `files` is always an array of relative paths. The Bun twin
//! is `cli/bun/src/core/service/run-records.ts`; behavior is pinned by
//! `cli/conformance/cases/service/run-records/`.
use super::fs::{FreshDirectory, create_new_file, valid_relative_path};
use super::http::{Request, TransportClass, encode_segment};
use super::{Context, Gate, manifest, program_ref, render};
use crate::error::{ErrorCode, human_safe_scalar};
use crate::{OutputMode, RunnerError};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::Path;

/// The completion marker `run download` writes last.
pub const DOWNLOAD_MARKER: &str = ".prose-run-manifest.json";
const DOWNLOAD_MARKER_PARTIAL: &str = ".prose-run-manifest.json.partial";

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.operation["id"].as_str().unwrap_or_default() {
        "run.list" => list(context),
        "run.show" => show(context),
        "run.download" => download(context),
        "run.share" => share(context),
        _ => context.not_implemented(),
    }
}

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn protocol(reason: impl Into<String>) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid).with_detail("reason", reason.into())
}

fn too_large(reason: impl Into<String>) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceResponseTooLarge).with_detail("reason", reason.into())
}

/// `^run_[A-Za-z0-9_-]{1,128}$`.
pub fn valid_run_id(value: &str) -> bool {
    value.strip_prefix("run_").is_some_and(|rest| {
        (1..=128).contains(&rest.len())
            && rest
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    })
}

/// A run id a person typed: `run_` and 1 to 128 lowercase hex digits, the
/// shape the service issues. Checked before any request is sent.
pub fn valid_run_id_argument(value: &str) -> bool {
    value.strip_prefix("run_").is_some_and(|rest| {
        (1..=128).contains(&rest.len())
            && rest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

/// Hex digits in a full run id; a shorter one was cut off when copied.
const FULL_RUN_ID_DIGITS: usize = 64;

/// A run id with fewer than 64 hex digits: `INVOCATION_INVALID` before any
/// request. The one run in this machine's journal it is a prefix of is the
/// correction; otherwise the recent runs listing (mirrors Bun
/// `truncatedRunId`).
fn truncated_run_id(context: &Context<'_>, value: &str) -> RunnerError {
    let error = invalid(format!(
        "run id {} looks truncated; a full run id has {FULL_RUN_ID_DIGITS} hex digits",
        crate::error::quote(value)
    ));
    let matches = context
        .journal()
        .entries()
        .into_iter()
        .filter_map(|entry| entry.run_id)
        .filter(|run| run != value && run.starts_with(value))
        .collect::<BTreeSet<_>>();
    if matches.len() == 1 {
        let full = matches.into_iter().next().unwrap_or_default();
        return context.corrected(
            error,
            &format!("Use the full run id {full}: `{{command}}`"),
            context.argv_with_argument(value, &full),
        );
    }
    let argv = context.follow_up_argv(&["run", "list", "--limit", "5"]);
    context.corrected(
        error,
        "List recent runs with `{command}` and copy the full run id",
        argv,
    )
}

/// The word that selects the caller's newest run in place of `RUN_ID`.
pub const LATEST_RUN: &str = "latest";

/// The `RUN_ID` argument with the `latest` selector: the newest run of the
/// caller's account (`GET /runs?limit=1`, the operation's GET /runs
/// request); any other value is [`run_id_argument`]. No runs is
/// `SERVICE_RESOURCE_NOT_FOUND` (mirrors Bun `runArgument`).
pub fn run_argument(context: &mut Context<'_>) -> Result<String, RunnerError> {
    if context.argument("RUN_ID") != Some(LATEST_RUN) {
        return run_id_argument(context);
    }
    let index = context.operation["requests"]
        .as_array()
        .and_then(|requests| {
            requests
                .iter()
                .position(|request| request["method"] == "GET" && request["path"] == "/runs")
        })
        .unwrap_or_default();
    let request = Request::from_manifest(context.operation, index, "/runs")
        .class(TransportClass::Control)
        .query("limit", "1");
    let body = context.send(&request)?.json_object()?;
    let rows = body
        .get("runs")
        .and_then(Value::as_array)
        .filter(|runs| runs.len() <= 200)
        .ok_or_else(|| protocol("the run list is missing or has more than 200 runs"))?;
    let Some(first) = rows.first() else {
        return Err(super::not_found::explain_not_found(
            RunnerError::catalog(ErrorCode::ServiceResourceNotFound).with_detail(
                "reason",
                "run latest was not found: this account has no runs yet",
            ),
            context.mode,
            "run",
            LATEST_RUN,
            Some(&["run", "list", "--limit", "5"]),
        ));
    };
    Ok(project_run(first, false)?["run_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned())
}

/// The `RUN_ID` argument, or `INVOCATION_INVALID` (nothing is sent) when it
/// is not a run id (identical in both ports).
pub fn run_id_argument(context: &Context<'_>) -> Result<String, RunnerError> {
    let value = context.argument("RUN_ID").unwrap_or_default();
    if valid_run_id_argument(value) {
        if value.len() - "run_".len() < FULL_RUN_ID_DIGITS {
            return Err(truncated_run_id(context, value));
        }
        return Ok(value.to_owned());
    }
    let error = invalid(format!(
        "run id {value_quoted} is invalid; a run id is run_ followed by lowercase hex digits; list runs with `{}`",
        context.command("run list"),
        value_quoted = crate::error::quote(value)
    ));
    // `4f1c...` without its prefix, or `RUN_4F1C...` in capitals.
    let lower = value.to_ascii_lowercase();
    let fixed = [lower.clone(), format!("run_{lower}")]
        .into_iter()
        .find(|candidate| valid_run_id_argument(candidate));
    Err(match fixed {
        Some(fixed) => context.corrected(
            error,
            &format!("Use the run id {fixed}: `{{command}}`"),
            context.argv_with_argument(value, &fixed),
        ),
        None => error,
    })
}

fn run_path(run_id: &str) -> String {
    format!("/runs/{}", encode_segment(run_id))
}

fn file_path(run_id: &str, relative: &str) -> String {
    let encoded = relative
        .split('/')
        .map(encode_segment)
        .collect::<Vec<_>>()
        .join("/");
    format!("/runs/{}/files/{encoded}", encode_segment(run_id))
}

/// A string of at most `max` code points without C0 controls or DEL (may be empty).
fn plain(value: &str, max: usize) -> bool {
    value.chars().count() <= max
        && !value
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
}

/// `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$`, at most 40 characters.
fn valid_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    let digits = |range: std::ops::Range<usize>| bytes[range].iter().all(u8::is_ascii_digit);
    bytes.len() <= 40
        && bytes.len() >= 12
        && digits(0..4)
        && bytes[4] == b'-'
        && digits(5..7)
        && bytes[7] == b'-'
        && digits(8..10)
        && bytes[10] == b'T'
        && bytes[bytes.len() - 1] == b'Z'
        && bytes[11..bytes.len() - 1]
            .iter()
            .all(|byte| byte.is_ascii_digit() || *byte == b':' || *byte == b'.')
}

/// `^[a-z0-9][a-z0-9.-]{0,63}$`.
fn valid_model(value: &str) -> bool {
    let bytes = value.as_bytes();
    let first = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
    (1..=64).contains(&bytes.len())
        && first(&bytes[0])
        && bytes
            .iter()
            .all(|byte| first(byte) || *byte == b'.' || *byte == b'-')
}

/// `owner/slug@<16 hex>`.
fn valid_program_ref(value: &str) -> bool {
    let Some((name, rev)) = value.split_once('@') else {
        return false;
    };
    let Some((owner, slug)) = name.split_once('/') else {
        return false;
    };
    program_ref::valid_owner(owner) && program_ref::valid_slug(slug) && program_ref::valid_rev(rev)
}

fn is_integer(value: &Value) -> bool {
    value.is_i64() || value.is_u64()
}

/// Copies the optional field `key` when `accept` admits it; `null` counts as
/// absent and anything else is a protocol violation.
fn optional(
    source: &Map<String, Value>,
    target: &mut Map<String, Value>,
    key: &str,
    accept: impl Fn(&Value) -> Option<Value>,
) -> Result<(), RunnerError> {
    match source.get(key) {
        None | Some(Value::Null) => Ok(()),
        Some(value) => {
            let projected = accept(value)
                .ok_or_else(|| protocol(format!("the run field {key} is malformed")))?;
            target.insert(key.to_owned(), projected);
            Ok(())
        }
    }
}

fn required_string(
    source: &Map<String, Value>,
    target: &mut Map<String, Value>,
    key: &str,
    accept: fn(&str) -> bool,
) -> Result<(), RunnerError> {
    match source.get(key).and_then(Value::as_str) {
        Some(value) if accept(value) => {
            target.insert(key.to_owned(), json!(value));
            Ok(())
        }
        _ => Err(protocol(format!(
            "the run field {key} is missing or malformed"
        ))),
    }
}

/// The run's environment as its public id (`builtin`, `linux`). The
/// service's version and runtime contract are not part of the record.
fn environment_provenance(value: &Value) -> Option<Value> {
    let id = value
        .as_object()?
        .get("id")?
        .as_str()
        .filter(|id| plain(id, 64))?;
    Some(json!(id))
}

fn repository(value: &Value) -> Option<Value> {
    let object = value.as_object()?;
    if object.get("provider")? != "github" {
        return None;
    }
    let owner = object
        .get("owner")?
        .as_str()
        .filter(|owner| plain(owner, 100))?;
    let name = object
        .get("name")?
        .as_str()
        .filter(|name| plain(name, 100))?;
    let mut projected = Map::new();
    projected.insert("provider".into(), json!("github"));
    projected.insert("owner".into(), json!(owner));
    projected.insert("name".into(), json!(name));
    match object.get("ref") {
        None | Some(Value::Null) => {}
        Some(reference) => {
            projected.insert(
                "ref".into(),
                json!(reference.as_str().filter(|value| plain(value, 256))?),
            );
        }
    }
    match object.get("writable") {
        None | Some(Value::Null) => {}
        Some(writable) => {
            projected.insert("writable".into(), json!(writable.as_bool()?));
        }
    }
    Some(Value::Object(projected))
}

fn repositories(value: &Value) -> Option<Value> {
    let items = value.as_array()?;
    if items.len() > 16 {
        return None;
    }
    items
        .iter()
        .map(repository)
        .collect::<Option<Vec<_>>>()
        .map(Value::Array)
}

fn usage(value: &Value) -> Option<Value> {
    let object = value.as_object()?;
    let input = object.get("input_tokens")?.as_u64()?;
    let output = object.get("output_tokens")?.as_u64()?;
    Some(json!({"input_tokens": input, "output_tokens": output}))
}

fn paths(value: &Value, key: &str) -> Result<Vec<String>, RunnerError> {
    let items = value
        .as_array()
        .ok_or_else(|| protocol(format!("the run field {key} is malformed")))?;
    if items.len() > max_files() {
        return Err(too_large(format!(
            "the run lists more than {} {key}",
            max_files()
        )));
    }
    items
        .iter()
        .map(|item| match item.as_str() {
            Some(path) if valid_relative_path(path) => Ok(path.to_owned()),
            _ => Err(protocol(format!(
                "the run manifest lists an unsafe path in {key}"
            ))),
        })
        .collect()
}

/// The run's `error` text: control characters become spaces, keys are
/// redacted, at most 4096 code points; blank text is dropped.
fn error_text(value: &str) -> Option<String> {
    let spaced = value
        .chars()
        .map(|character| {
            if character <= '\u{1f}' || character == '\u{7f}' {
                ' '
            } else {
                character
            }
        })
        .collect::<String>();
    let cut = render::redact(&spaced, None)
        .chars()
        .take(4096)
        .collect::<String>();
    (!cut.chars().all(|character| character == ' ')).then_some(cut)
}

/// Projects a run summary (`manifest == false`) or a run manifest.
pub fn project_run(source: &Value, manifest: bool) -> Result<Value, RunnerError> {
    let source = source
        .as_object()
        .ok_or_else(|| protocol("a run record is not an object"))?;
    let mut target = Map::new();
    required_string(source, &mut target, "run_id", valid_run_id)?;
    required_string(source, &mut target, "created_at", valid_timestamp)?;
    required_string(source, &mut target, "status", |value| plain(value, 32))?;
    required_string(source, &mut target, "model", valid_model)?;
    optional(source, &mut target, "environment", environment_provenance)?;
    for key in ["price_cents", "environment_price_cents"] {
        optional(source, &mut target, key, |value| {
            is_integer(value).then(|| value.clone())
        })?;
    }
    optional(source, &mut target, "billing_status", |value| {
        value
            .as_str()
            .filter(|text| plain(text, 32))
            .map(|text| Value::from(render::public_billing(text)))
    })?;
    optional(source, &mut target, "program_ref", |value| {
        value
            .as_str()
            .filter(|text| valid_program_ref(text))
            .map(Value::from)
    })?;
    optional(source, &mut target, "repositories", repositories)?;
    if manifest {
        let files = paths(
            source
                .get("files")
                .ok_or_else(|| protocol("the run field files is missing or malformed"))?,
            "files",
        )?;
        target.insert("files".into(), json!(files));
        let has_patch = source
            .get("has_patch")
            .and_then(Value::as_bool)
            .ok_or_else(|| protocol("the run field has_patch is missing or malformed"))?;
        target.insert("has_patch".into(), json!(has_patch));
        optional(source, &mut target, "usage", usage)?;
        if let Some(value) = source.get("session_files").filter(|value| !value.is_null()) {
            target.insert(
                "session_files".into(),
                json!(paths(value, "session_files")?),
            );
        }
        match source.get("error") {
            None | Some(Value::Null) => {}
            Some(Value::String(text)) => {
                if let Some(text) = error_text(text) {
                    target.insert("error".into(), json!(render::run_error_text(&text)));
                }
            }
            Some(_) => return Err(protocol("the run field error is malformed")),
        }
    }
    Ok(Value::Object(target))
}

fn download_class(key: &str) -> u64 {
    manifest()["transportClasses"]["download"][key]
        .as_u64()
        .unwrap_or(0)
}

fn max_files() -> usize {
    usize::try_from(download_class("maxFiles")).unwrap_or(10_000)
}

/// Download limits `(per file, per run)`. Test-seam builds may lower them
/// with `PROSE_TEST_SERVICE_DOWNLOAD_LIMITS=<file bytes>,<run bytes>` so the
/// caps are testable without 64 MiB fixtures; ordinary builds ignore it.
fn download_limits(context: &Context<'_>) -> (u64, u64) {
    let limits = (
        download_class("maxFileBytes"),
        download_class("maxRunBytes"),
    );
    #[cfg(feature = "test-seams")]
    if let Some((file, run)) = context
        .system
        .environment
        .get("PROSE_TEST_SERVICE_DOWNLOAD_LIMITS")
        .and_then(|value| value.split_once(','))
        .and_then(|(file, run)| Some((file.parse::<u64>().ok()?, run.parse::<u64>().ok()?)))
    {
        return (limits.0.min(file), limits.1.min(run));
    }
    let _ = context;
    limits
}

fn fetch_manifest(context: &mut Context<'_>, run_id: &str) -> Result<Value, RunnerError> {
    let request = Request::from_manifest(context.operation, 0, run_path(run_id))
        .class(TransportClass::Control);
    let body = context.send(&request)?.json_object()?;
    project_run(&Value::Object(body), true)
}

fn manifest_files(manifest: &Value) -> Vec<String> {
    manifest["files"]
        .as_array()
        .map(|files| {
            files
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

/// Refuses an existing destination or a missing parent before any request,
/// with the fresh-writer messages. `defaulted` marks `run download`'s default
/// `./RUN_ID` directory, whose refusal names `--output-dir`.
fn preflight_destination(
    cwd: &Path,
    value: &str,
    directory: bool,
    defaulted: bool,
) -> Result<(), RunnerError> {
    let path = cwd.join(value);
    if std::fs::symlink_metadata(&path).is_ok() && defaulted {
        return Err(invalid(format!(
            "output directory {value_quoted} (the default, ./RUN_ID) already exists; pass --output-dir DIR to choose a new directory",
            value_quoted = crate::error::quote(value)
        )));
    }
    if std::fs::symlink_metadata(&path).is_ok() {
        return Err(invalid(if directory {
            format!(
                "output directory {value_quoted} already exists; choose a new directory",
                value_quoted = crate::error::quote(value)
            )
        } else {
            format!(
                "output file {value_quoted} already exists; choose a new path",
                value_quoted = crate::error::quote(value)
            )
        }));
    }
    if !path.parent().is_some_and(Path::is_dir) {
        return Err(invalid(if directory {
            format!(
                "the parent of output directory {value_quoted} does not exist",
                value_quoted = crate::error::quote(value)
            )
        } else {
            format!(
                "the directory for output file {value_quoted} does not exist",
                value_quoted = crate::error::quote(value)
            )
        }));
    }
    Ok(())
}

// ---------------------------------------------------------------- run list

fn parse_limit(value: &str) -> Option<u32> {
    if value.is_empty() || value.len() > 3 || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    value
        .parse::<u32>()
        .ok()
        .filter(|limit| (1..=200).contains(limit))
}

fn list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let raw_limit = context.option("--limit").unwrap_or("20");
    let limit = parse_limit(raw_limit).ok_or_else(|| {
        context.limit_error(
            invalid(format!(
                "--limit {raw_limit_quoted} is invalid; use a whole number from 1 to 200",
                raw_limit_quoted = crate::error::quote(raw_limit)
            )),
            raw_limit,
            200,
        )
    })?;
    let before = context.option("--before").map(str::to_owned);
    if let Some(cursor) = &before {
        if cursor.is_empty() || !plain(cursor, 512) {
            return Err(invalid(
                "--before must be the nextBefore value of a previous `cli run list` result",
            ));
        }
    }
    let mut request =
        Request::from_manifest(context.operation, 0, "/runs").query("limit", limit.to_string());
    if let Some(cursor) = &before {
        request = request.query("before", cursor.clone());
    }
    let body = context.send(&request)?.json_object()?;
    let runs = body
        .get("runs")
        .and_then(Value::as_array)
        .filter(|runs| runs.len() <= 200)
        .ok_or_else(|| protocol("the run list is missing or has more than 200 runs"))?
        .iter()
        .map(|run| project_run(run, false).map(|projected| mark_cancelled(projected, Some(run))))
        .collect::<Result<Vec<_>, _>>()?;
    context.next_before = match body.get("next_before") {
        None | Some(Value::Null) => None,
        Some(Value::String(cursor)) if !cursor.is_empty() && plain(cursor, 512) => {
            Some(cursor.clone())
        }
        Some(_) => return Err(protocol("the run list cursor is malformed")),
    };
    let mut human = String::new();
    if runs.is_empty() {
        human.push_str("No runs.\n");
    }
    for run in &runs {
        let _ = writeln!(
            human,
            "{}  {}  {}  {}",
            run["run_id"].as_str().unwrap_or_default(),
            if run["cancelled"] == true {
                "cancelled"
            } else {
                run["status"].as_str().unwrap_or_default()
            },
            run["model"].as_str().unwrap_or_default(),
            run["created_at"].as_str().unwrap_or_default()
        );
    }
    context.human = Some(human);
    Ok(json!({ "runs": runs }))
}

// ---------------------------------------------------------------- run show

/// UTF-8 text the `text` schema admits (tab, LF and CR are the only controls).
fn text_content(bytes: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(bytes).ok()?;
    let admitted = !text.chars().any(|character| {
        (character <= '\u{1f}' && !matches!(character, '\t' | '\n' | '\r')) || character == '\u{7f}'
    });
    (admitted && text.chars().count() <= 1_048_576).then(|| text.to_owned())
}

fn content_type(headers: &std::collections::BTreeMap<String, String>) -> Option<String> {
    headers
        .get("content-type")
        .filter(|value| !value.is_empty() && plain(value, 128))
        .cloned()
}

fn show(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    if context.argument("RUN_ID") != Some(LATEST_RUN) {
        run_id_argument(context)?;
    }
    let file = context.option("--file").map(str::to_owned);
    let output_file = context.option("--output-file").map(str::to_owned);
    if output_file.is_some() && file.is_none() {
        return Err(invalid(
            "--output-file needs --file PATH; `cli run download RUN_ID --output-dir DIR` saves every file",
        ));
    }
    if let Some(path) = &file {
        if !valid_relative_path(path) {
            return Err(invalid(format!(
                "--file {path_quoted} must be a relative path exactly as listed in the run manifest",
                path_quoted = crate::error::quote(path)
            )));
        }
    }
    let cwd = context.system.current_dir.clone();
    if let Some(value) = &output_file {
        preflight_destination(&cwd, value, false, false)?;
    }
    let run_id = run_argument(context)?;
    let manifest = mark_cancelled(fetch_manifest(context, &run_id)?, None);
    let Some(path) = file else {
        context.human = Some(human_run(&manifest));
        // The run record is `result.run`, as in `run submit` and `run watch`.
        return Ok(json!({"runId": run_id, "run": manifest}));
    };
    let files = manifest_files(&manifest);
    if !files.contains(&path) {
        // A file the run does not list is not found (like a run id the
        // service does not know). `--file part6.md` for `outputs/part6.md`:
        // the one listed file whose path ends with the typed one is the
        // correction.
        let error = RunnerError::catalog(ErrorCode::ServiceResourceNotFound)
            .with_detail(
                "reason",
                format!(
                    "run {run_id} has no file {path_quoted}; list its files with `{}`",
                    context.command(&format!("run show {run_id}")),
                    path_quoted = crate::error::quote(&path)
                ),
            )
            .with_detail(
                "resource",
                json!({"kind": "file", "id": format!("{run_id}/{path}")}),
            );
        let suffix = format!("/{path}");
        let matches = files
            .iter()
            .filter(|file| file.ends_with(&suffix))
            .collect::<Vec<_>>();
        return Err(match matches.as_slice() {
            [file] => context.corrected(
                error,
                &format!("Use the listed path {file}: `{{command}}`"),
                context.argv_with_option("--file", file),
            ),
            _ => file_not_found(context, &run_id, &path, error),
        });
    }
    let request = Request::from_manifest(context.operation, 1, file_path(&run_id, &path));
    let file_result = if let Some(value) = &output_file {
        let (max_file, _) = download_limits(context);
        let mut sink = create_new_file(&cwd, value)?;
        let downloaded = context
            .download(&request, &mut sink, max_file)
            .and_then(|downloaded| {
                sink.sync_all().map_err(|_| {
                    invalid(format!(
                        "cannot write output file {value_quoted}",
                        value_quoted = crate::error::quote(value)
                    ))
                })?;
                Ok(downloaded)
            });
        let downloaded = match downloaded {
            Ok(downloaded) => downloaded,
            Err(error) => {
                drop(sink);
                let _ = std::fs::remove_file(cwd.join(value));
                return Err(file_not_found(
                    context,
                    &run_id,
                    &path,
                    annotate_too_large(error, || {
                        format!(
                            "{path_quoted} is larger than {max_file} bytes",
                            path_quoted = crate::error::quote(&path)
                        )
                    }),
                ));
            }
        };
        let mut file =
            json!({"bytes": downloaded.bytes, "sha256": downloaded.sha256, "written": true});
        if let Some(kind) = content_type(&downloaded.headers) {
            file["contentType"] = json!(kind);
        }
        context.human = Some(format!(
            "Saved {} bytes from {path} (sha256 {})\n",
            downloaded.bytes, downloaded.sha256
        ));
        file
    } else {
        let limit = TransportClass::Control.max_response_bytes();
        let mut sink = Vec::new();
        let downloaded = context
            .download(&request, &mut sink, limit)
            .map_err(|error| {
                annotate_too_large(error, || {
                    format!(
                        "{path_quoted} is larger than {limit} bytes; pass --output-file FILE to save it", path_quoted = crate::error::quote(&path))
                })
            })
            .map_err(|error| file_not_found(context, &run_id, &path, error))?;
        let mut file =
            json!({"bytes": downloaded.bytes, "sha256": downloaded.sha256, "written": false});
        if let Some(kind) = content_type(&downloaded.headers) {
            file["contentType"] = json!(kind);
        }
        if let Some(text) = text_content(&sink) {
            // Human output ends with a line feed; JSON keeps the file's bytes.
            context.human = Some(if text.is_empty() || text.ends_with('\n') {
                text.clone()
            } else {
                format!("{text}\n")
            });
            file["content"] = json!(text);
        } else {
            context.human = Some(format!(
                "{path} is {} bytes of binary data (sha256 {}); pass --output-file FILE to save it\n",
                downloaded.bytes, downloaded.sha256
            ));
        }
        file
    };
    Ok(json!({"runId": run_id, "path": path, "file": file_result}))
}

/// The service's error text for a run its owner stopped.
const USER_STOPPED: &str = "Stopped by the user.";

/// Whether the service's run error is the one it records when the owner
/// stopped the run.
pub fn stopped_by_owner(error: &Value) -> bool {
    error
        .as_str()
        .is_some_and(|error| error.trim() == USER_STOPPED)
}

/// Adds `cancelled: true` to a projected run whose owner stopped it (the
/// service records it as `error` with its stop reason) or whose status is
/// cancelled, so `run list` and `run show` agree with `run watch`'s exit 24.
/// `source` is the service's record (a run list row carries the reason
/// without the CLI projecting it); without it the projection's own `error`
/// is read. Mirrors Bun `markCancelled`.
pub fn mark_cancelled(mut run: Value, source: Option<&Value>) -> Value {
    let status = run["status"].as_str().unwrap_or_default();
    let error = source.map_or(&run["error"], |source| &source["error"]);
    if matches!(status, "cancelled" | "canceled") || stopped_by_owner(error) {
        run["cancelled"] = json!(true);
    }
    run
}

/// The human `run show` status. The status word is the service's in every
/// command (`run list` and `service triage` list records without the stop
/// reason, so they cannot tell a stopped run apart); `run show` has the
/// reason and adds it: `error (stopped by you)`. JSON keeps the record.
fn shown_status(status: &str, error: &Value) -> String {
    if stopped_by_owner(error) {
        "cancelled (stopped by you)".to_owned()
    } else {
        status.to_owned()
    }
}

/// A run record's status word for `run cancel` of an ended run: `cancelled`
/// for a run its owner stopped (the service records it as `error` with its
/// stop reason), otherwise the record's status.
pub fn ended_status(record: &Value) -> Option<String> {
    let status = record["status"].as_str()?;
    Some(if stopped_by_owner(&record["error"]) {
        "cancelled".to_owned()
    } else {
        status.to_owned()
    })
}

/// The human billing state ([`render::billing_label`]).
fn shown_billing(billing: &str, price_cents: Option<i64>) -> String {
    render::billing_label(billing, price_cents)
}

/// Run statuses after which nothing more happens (any other status can still
/// be watched).
const ENDED_STATUSES: [&str; 5] = ["completed", "failed", "error", "cancelled", "canceled"];

/// Readable `run show` output: one `label: value` line per
/// field, prices in dollars, nested records flattened, files one per line,
/// then copyable `Next:` commands in this environment. JSON is unchanged.
fn human_run(run: &Value) -> String {
    let text_of = |value: &Value| value.as_str().map(human_safe_scalar).unwrap_or_default();
    let id = run["run_id"].as_str().unwrap_or_default();
    let mut text = format!("Run {}\n", human_safe_scalar(id));
    let line = |text: &mut String, label: &str, value: String| {
        if !value.is_empty() {
            let _ = writeln!(text, "  {label}: {value}");
        }
    };
    let status = shown_status(run["status"].as_str().unwrap_or_default(), &run["error"]);
    line(&mut text, "status", human_safe_scalar(&status));
    line(&mut text, "model", text_of(&run["model"]));
    line(&mut text, "created", text_of(&run["created_at"]));
    line(&mut text, "program", text_of(&run["program_ref"]));
    if let Some(cents) = run["price_cents"].as_i64() {
        let environment_price = run["environment_price_cents"]
            .as_i64()
            .map(|cents| format!(" (environment {})", render::dollars(cents)))
            .unwrap_or_default();
        line(
            &mut text,
            "price",
            format!("{}{environment_price}", render::dollars(cents)),
        );
    }
    line(
        &mut text,
        "billing",
        human_safe_scalar(&shown_billing(
            run["billing_status"].as_str().unwrap_or_default(),
            run["price_cents"].as_i64(),
        )),
    );
    if let (Some(input), Some(output)) = (
        run["usage"]["input_tokens"].as_i64(),
        run["usage"]["output_tokens"].as_i64(),
    ) {
        line(
            &mut text,
            "usage",
            format!("{input} input tokens, {output} output tokens"),
        );
    }
    line(&mut text, "environment", text_of(&run["environment"]));
    for repository in run["repositories"]
        .as_array()
        .map_or(&[][..], Vec::as_slice)
    {
        let reference = repository["ref"]
            .as_str()
            .map(|reference| format!("@{}", human_safe_scalar(reference)))
            .unwrap_or_default();
        let writable = if repository["writable"] == true {
            " (writable)"
        } else {
            ""
        };
        line(
            &mut text,
            "repository",
            format!(
                "{}/{}{reference}{writable}",
                text_of(&repository["owner"]),
                text_of(&repository["name"])
            ),
        );
    }
    if run["output"].is_object() {
        let output = &run["output"];
        line(
            &mut text,
            "output",
            format!(
                "commit {} on {} {} ({} changed files) {}",
                text_of(&output["commit_sha"]),
                text_of(&output["repository"]),
                text_of(&output["branch"]),
                output["changed_files"].as_array().map_or(0, Vec::len),
                text_of(&output["url"])
            ),
        );
    }
    if !stopped_by_owner(&run["error"]) {
        line(&mut text, "error", text_of(&run["error"]));
    }
    if run["has_patch"] == true {
        line(&mut text, "repository changes", "yes".to_owned());
    }
    let files = manifest_files(run);
    for (label, list) in [
        ("files", files.clone()),
        ("session files", {
            run["session_files"]
                .as_array()
                .map(|paths| {
                    paths
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        }),
    ] {
        if list.is_empty() {
            if label == "files" {
                text.push_str("  files: none\n");
            }
            continue;
        }
        let _ = writeln!(text, "  {label} ({}):", list.len());
        for path in &list {
            let _ = writeln!(text, "    {}", human_safe_scalar(path));
        }
    }
    let status = run["status"].as_str().unwrap_or_default();
    if !ENDED_STATUSES.contains(&status) {
        text.push_str(&render::next_line(&["run", "watch", id]));
    }
    if let Some(first) = files.first() {
        text.push_str(&render::next_line(&["run", "show", id, "--file", first]));
        text.push_str(&render::next_line(&["run", "download", id]));
    }
    text
}

/// A 404 on one of the run's files names the file, not the run.
fn file_not_found(
    context: &Context<'_>,
    run_id: &str,
    path: &str,
    error: RunnerError,
) -> RunnerError {
    super::not_found::explain_not_found(
        error,
        context.mode,
        "file",
        &format!("{run_id}/{path}"),
        Some(&["run", "show", run_id]),
    )
}

fn annotate_too_large(error: RunnerError, reason: impl FnOnce() -> String) -> RunnerError {
    if error.code == ErrorCode::ServiceResponseTooLarge && !has_reason(&error) {
        error.with_detail("reason", reason())
    } else {
        error
    }
}

/// Whether the error already has a specific reason; the generic transport
/// reason gives way to the download's own.
fn has_reason(error: &RunnerError) -> bool {
    error
        .details
        .as_ref()
        .and_then(|details| details.get("reason"))
        .is_some_and(|reason| reason != super::http::TRANSPORT_REASON)
}

// ------------------------------------------------------------ run download

/// Paths must be unique, must not nest under another listed file, and must
/// not take the marker's names.
fn check_download_paths(files: &[String]) -> Result<(), RunnerError> {
    let mut seen = BTreeSet::new();
    for path in files {
        if path == DOWNLOAD_MARKER || path == DOWNLOAD_MARKER_PARTIAL {
            return Err(protocol(format!(
                "the run manifest lists the reserved path {path_quoted}",
                path_quoted = crate::error::quote(path)
            )));
        }
        if !seen.insert(path.as_str()) {
            return Err(protocol(format!(
                "the run manifest lists {path_quoted} more than once",
                path_quoted = crate::error::quote(path)
            )));
        }
    }
    for path in files {
        let prefix = format!("{path}/");
        if files.iter().any(|other| other.starts_with(&prefix)) {
            return Err(protocol(format!(
                "the run manifest lists {path_quoted} as both a file and a directory",
                path_quoted = crate::error::quote(path)
            )));
        }
    }
    Ok(())
}

/// When `directory` exists, the first of `DIRECTORY-2` .. `DIRECTORY-99`
/// that does not.
fn free_directory(cwd: &Path, directory: &str) -> Option<String> {
    let base = directory.trim_end_matches('/');
    if base.is_empty() || std::fs::symlink_metadata(cwd.join(base)).is_err() {
        return None;
    }
    (2..100)
        .map(|index| format!("{base}-{index}"))
        .find(|candidate| std::fs::symlink_metadata(cwd.join(candidate)).is_err())
}

fn download(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    if context.argument("RUN_ID") != Some(LATEST_RUN) {
        run_id_argument(context)?;
    }
    let run_id = run_argument(context)?;
    let given = context.option("--output-dir").map(str::to_owned);
    let defaulted = given.is_none();
    // Without --output-dir the files go to ./RUN_ID (a run id is a safe
    // directory name).
    let directory = given.unwrap_or_else(|| run_id.clone());
    let cwd = context.system.current_dir.clone();
    preflight_destination(&cwd, &directory, true, defaulted).map_err(|error| {
        // An existing directory names a free one to use.
        match free_directory(&cwd, &directory) {
            Some(free) => context.corrected(
                error,
                &format!("Download into the new directory {free}: `{{command}}`"),
                context.argv_setting_option("--output-dir", &free),
            ),
            None => error,
        }
    })?;
    let manifest = fetch_manifest(context, &run_id)?;
    let files = manifest_files(&manifest);
    check_download_paths(&files)?;
    let (max_file, max_run) = download_limits(context);
    let fresh = FreshDirectory::create(&cwd, &directory)?;
    let incomplete = format!("the output directory is incomplete and has no {DOWNLOAD_MARKER}");
    let mut total: u64 = 0;
    let mut entries = Vec::with_capacity(files.len());
    for path in &files {
        let remaining = max_run.saturating_sub(total);
        let cap = max_file.min(remaining);
        let request = Request::from_manifest(context.operation, 1, file_path(&run_id, path));
        let result = fresh.create_file(path).and_then(|mut sink| {
            let downloaded = context.download(&request, &mut sink, cap)?;
            sink.sync_all()
                .map_err(|_| invalid("cannot write the downloaded file"))?;
            Ok(downloaded)
        });
        let downloaded = result.map_err(|error| {
            if has_reason(&error) {
                return error;
            }
            let cause = if error.code == ErrorCode::ServiceResponseTooLarge {
                if cap < max_file {
                    format!("the run's files exceed {max_run} bytes in total")
                } else {
                    format!(
                        "{path_quoted} is larger than {max_file} bytes",
                        path_quoted = crate::error::quote(path)
                    )
                }
            } else {
                format!(
                    "downloading {path_quoted} failed",
                    path_quoted = crate::error::quote(path)
                )
            };
            file_not_found(
                context,
                &run_id,
                path,
                error.with_detail("reason", format!("{cause}; {incomplete}")),
            )
        })?;
        total += downloaded.bytes;
        entries.push(json!({"path": path, "bytes": downloaded.bytes, "sha256": downloaded.sha256}));
    }
    let result = json!({
        "runId": run_id,
        "outputDir": directory,
        "fileCount": entries.len(),
        "totalBytes": total,
        "files": entries,
        "marker": DOWNLOAD_MARKER,
    });
    write_marker(&fresh, &json!({"download": result, "run": manifest}))?;
    let mut human = format!(
        "Downloaded {} file{} ({total} bytes) from {run_id} into {directory_quoted}\n",
        files.len(),
        if files.len() == 1 { "" } else { "s" },
        directory_quoted = crate::error::quote(&directory)
    );
    for entry in result["files"].as_array().into_iter().flatten() {
        let _ = writeln!(
            human,
            "  {} ({} bytes)",
            entry["path"].as_str().unwrap_or_default(),
            entry["bytes"]
        );
    }
    let _ = writeln!(human, "  {DOWNLOAD_MARKER} (the download record)");
    context.human = Some(human);
    Ok(result)
}

/// Writes the completion marker under a temporary name, then links it into
/// place without replacing anything (a rename where links are unsupported;
/// the directory is this invocation's own).
fn write_marker(fresh: &FreshDirectory, document: &Value) -> Result<(), RunnerError> {
    let failed = || invalid(format!("cannot write {DOWNLOAD_MARKER}"));
    let mut bytes = render::canonical(document).into_bytes();
    bytes.push(b'\n');
    let mut file = fresh.create_file(DOWNLOAD_MARKER_PARTIAL)?;
    file.write_all(&bytes)
        .and_then(|()| file.sync_all())
        .map_err(|_| failed())?;
    drop(file);
    let temporary = fresh.root().join(DOWNLOAD_MARKER_PARTIAL);
    let target = fresh.root().join(DOWNLOAD_MARKER);
    match std::fs::hard_link(&temporary, &target) {
        Ok(()) => std::fs::remove_file(&temporary).map_err(|_| failed()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Err(failed()),
        Err(_) => std::fs::rename(&temporary, &target).map_err(|_| failed()),
    }
}

// --------------------------------------------------------------- run share

/// 9999-12-31T23:59:59Z: later expiries would need an expanded year form.
const MAX_EXPIRY_SECONDS: i64 = 253_402_300_799;

/// The `exp` (epoch seconds) of the share URL's fragment, as RFC 3339.
fn share_expiry(url: &str) -> Option<String> {
    let (_, fragment) = url.split_once('#')?;
    let seconds = fragment
        .split('&')
        .find_map(|pair| pair.strip_prefix("exp="))
        .filter(|value| {
            (1..=12).contains(&value.len()) && value.bytes().all(|b| b.is_ascii_digit())
        })?
        .parse::<i64>()
        .ok()
        .filter(|seconds| *seconds <= MAX_EXPIRY_SECONDS)?;
    chrono::DateTime::from_timestamp(seconds, 0)
        .map(|time| time.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
}

fn valid_share_url(url: &str) -> bool {
    url.len() <= 2048
        && url.len() > "https://".len()
        && url.starts_with("https://")
        && url.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

fn share(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let run_id = run_argument(context)?;
    let path = format!("{}/share", run_path(&run_id));
    let planned = context.planned(0, &path, &[], None);
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let request = Request::from_manifest(context.operation, 0, path);
    let body = context.send(&request)?.json_object()?;
    let url = body
        .get("url")
        .and_then(Value::as_str)
        .filter(|url| valid_share_url(url))
        .ok_or_else(|| protocol("the share response has no valid https url"))?
        .to_owned();
    let mut result = json!({"runId": run_id, "url": url});
    let expires = share_expiry(&url);
    if let Some(expires) = &expires {
        result["expires_at"] = json!(expires);
        result["expiresAt"] = json!(expires);
    }
    if context.mode == OutputMode::Human {
        let _ = writeln!(
            context.err,
            "Warning: anyone with this link can read this run's outputs for 24 hours, and it cannot be revoked. To give specific people access instead, invite them to your organization (`cli org invite`)."
        );
        let expiry = expires
            .map(|expires| format!("Expires: {expires}\n"))
            .unwrap_or_default();
        context.human = Some(format!("{url}\n{expiry}"));
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_ids_and_timestamps() {
        assert!(valid_run_id("run_abc-DEF_1"));
        for bad in ["run_", "run", "garbage", "run_a/b", "run_a b"] {
            assert!(!valid_run_id(bad), "{bad}");
        }
        assert!(valid_run_id(&format!("run_{}", "a".repeat(128))));
        assert!(!valid_run_id(&format!("run_{}", "a".repeat(129))));
        assert!(valid_timestamp("2026-09-23T20:29:47.788Z"));
        assert!(!valid_timestamp("2026-09-23 20:29:47Z"));
        assert!(!valid_timestamp("2026-09-23T20:29:47+00:00"));
    }

    #[test]
    fn manifests_drop_signed_urls_and_unknown_fields() {
        let projected = project_run(
            &json!({
                "run_id": "run_1", "customer_id": "cus_x", "status": "completed",
                "model": "model-sol", "created_at": "2026-09-23T20:29:47.788Z",
                "files": ["outputs/result.json"], "has_patch": false,
                "file_urls": {"outputs/result.json": "https://x/?tok=secret"},
                "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3},
                "repositories": [{"provider": "github", "owner": "o", "name": "n", "ref": "main", "repository_id": 5, "commit_sha": "ab", "writable": true}],
                "error": "line one\nline two",
                "spec_commit": "abc"
            }),
            true,
        )
        .unwrap();
        let text = projected.to_string();
        assert!(!text.contains("tok=") && !text.contains("cus_x") && !text.contains("spec_commit"));
        // Run error text prints its mapped words; unknown text the generic
        // message.
        assert_eq!(projected["error"], "The run failed on the service.");
        assert_eq!(
            projected["usage"],
            json!({"input_tokens": 1, "output_tokens": 2})
        );
        assert_eq!(
            projected["repositories"][0],
            json!({"provider": "github", "owner": "o", "name": "n", "ref": "main", "writable": true})
        );
    }

    #[test]
    fn unsafe_or_conflicting_paths_are_protocol_errors() {
        let manifest = |files: Value| {
            json!({"run_id": "run_1", "status": "completed", "model": "m",
                   "created_at": "2026-09-23T20:29:47Z", "files": files, "has_patch": false})
        };
        for bad in [
            json!(["../x"]),
            json!(["/etc/passwd"]),
            json!(["a\\b"]),
            json!([1]),
        ] {
            let error = project_run(&manifest(bad), true).unwrap_err();
            assert_eq!(error.code, ErrorCode::ServiceProtocolInvalid);
        }
        let strings = |values: &[&str]| values.iter().map(|v| (*v).to_owned()).collect::<Vec<_>>();
        assert!(check_download_paths(&strings(&["a", "b/c"])).is_ok());
        for bad in [&["a", "a"][..], &["a", "a/b"], &[DOWNLOAD_MARKER]] {
            assert!(check_download_paths(&strings(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn limits_cursors_and_share_urls() {
        assert_eq!(parse_limit("200"), Some(200));
        for bad in ["0", "201", "-1", "+5", "1e2", "", "0200"] {
            assert_eq!(parse_limit(bad), None, "{bad}");
        }
        assert_eq!(
            share_expiry("https://web/share/runs/run_1#tok=abc&exp=1790298569").as_deref(),
            Some("2026-09-25T01:09:29Z")
        );
        assert_eq!(share_expiry("https://web/share/runs/run_1"), None);
        assert!(valid_share_url("https://web/share/runs/run_1#tok=a&exp=1"));
        assert!(!valid_share_url("http://web/x"));
        assert!(!valid_share_url("https://web/a b"));
        assert_eq!(
            file_path("run_1", "outputs/a b.txt"),
            "/runs/run_1/files/outputs/a%20b.txt"
        );
    }
}
