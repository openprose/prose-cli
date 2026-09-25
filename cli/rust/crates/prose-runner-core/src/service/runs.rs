//! Service run lifecycle: `cli run quote|submit|watch|input|cancel`.
//!
//! Runs are always live sessions (decision 6): `run submit` journals a session
//! UUID before `POST /run?live=1&session=S`, resubmits once with the same
//! session when the connection drops before the first event, and treats the
//! service's 409 as proof of admission (recovering the run id from `X-Run-Id`
//! or a `run_id` body field when present, otherwise `RUN_SUBMISSION_AMBIGUOUS`).
//! Only the duplicate-session 409 is treated that way; any other 409 is
//! classified normally. Interrupts, lost streams and the `--wait`
//! deadline detach and never cancel (decision 8): they report
//! `HOSTED_RUN_DETACHED` or `SERVICE_WATCH_DEADLINE` with `details.resumeArgv`,
//! never a retry of the submit. Only `run cancel --yes` cancels, and
//! a service-side cancel is the terminal `HOSTED_RUN_CANCELLED`. Behavior is pinned by the shared corpus in
//! `cli/conformance/cases/service/runs/` and documented in `docs/service/runs.md`.
use super::http::{Request, Response, StreamOpen, TransportClass};
use super::journal::{Entry, valid_session};
use super::run_records::{LATEST_RUN, project_run, run_argument, stopped_by_owner};
use super::sse::{SseItem, SseReader, StreamEnd};
use super::{Context, Gate, program_ref, render};
use crate::error::{ErrorCode, human_safe_scalar};
use crate::{OutputMode, RunnerError};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// A program source (`FILE` or `-`) is at most 1 MiB of UTF-8.
const MAX_PROGRAM_BYTES: u64 = 1 << 20;
/// `--input K=@FILE` and `--inputs-file` are at most 1 MiB each.
const MAX_INPUT_FILE_BYTES: u64 = 1 << 20;
/// The whole JSON submission is at most 8 MiB.
const MAX_BODY_BYTES: usize = 8 << 20;
/// `run input` text: 1 to 4000 UTF-16 code units (the service's own measure).
const MAX_INSTRUCTION_UNITS: usize = 4000;
const PRUNE_DAYS: i64 = 30;
/// Sequences and counts stay within JSON's exact integer range (as in Bun).
const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.operation["id"].as_str() {
        Some("run.quote") => quote(context),
        Some("run.submit") => submit(context),
        Some("run.watch") => watch(context),
        Some("run.input") => input(context),
        Some("run.cancel") => cancel(context),
        _ => context.not_implemented(),
    }
}

// ---------------------------------------------------------------------------
// Validation and sanitation helpers (identical rules in the Bun product).

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn protocol(reason: impl Into<String>) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid).with_detail("reason", reason.into())
}

/// A failure whose Action names the one next command: the
/// Action quotes it and `details.suggestedArgv` carries it; never retryable.
fn with_next_command(
    context: &Context<'_>,
    mut error: RunnerError,
    action: &str,
    words: &[&str],
) -> RunnerError {
    error.action = action.replace("{command}", &command(context, &words.join(" ")));
    error.retryable = false;
    error.with_detail("suggestedArgv", context.follow_up_argv(words))
}

/// Whether a failure is the service's 409 with exactly this message
/// (ended-run refusals).
fn conflict_saying(error: &RunnerError, message: &str) -> bool {
    error.code == ErrorCode::ServiceWriteConflict
        && error.details.as_ref().is_some_and(|details| {
            details.get("serviceStatus") == Some(&json!(409))
                && details.get("serviceMessage").and_then(Value::as_str) == Some(message)
        })
}

/// Run record statuses that mean the run is over. A record is written only
/// when a run ends (completed, error or timeout today); the others are
/// accepted so a service that starts writing them is still read correctly.
const ENDED_STATUSES: [&str; 6] = [
    "completed",
    "error",
    "failed",
    "timeout",
    "cancelled",
    "canceled",
];

fn is_control(character: char) -> bool {
    character <= '\u{1f}' || character == '\u{7f}'
}

/// One-line text: every C0 control and DEL becomes a space; at most `max`
/// code points.
fn clean_line(value: &str, max: usize) -> String {
    value
        .chars()
        .map(|character| {
            if is_control(character) {
                ' '
            } else {
                character
            }
        })
        .take(max)
        .collect()
}

/// Multi-line text: like [`clean_line`] but TAB, LF and CR are kept.
fn clean_text(value: &str, max: usize) -> String {
    value
        .chars()
        .map(|character| {
            if is_control(character) && !matches!(character, '\t' | '\n' | '\r') {
                ' '
            } else {
                character
            }
        })
        .take(max)
        .collect()
}

/// Terminal-safe program text for human mode: also blanks C1 controls.
fn terminal_text(value: &str) -> String {
    clean_text(value, usize::MAX)
        .chars()
        .map(|character| {
            if ('\u{80}'..='\u{9f}').contains(&character) {
                ' '
            } else {
                character
            }
        })
        .collect()
}

fn ascii_all(value: &str, allowed: impl Fn(u8) -> bool) -> bool {
    value.bytes().all(allowed)
}

/// `^[a-z0-9][a-z0-9_-]{0,63}$` (environment and runtime ids).
fn valid_token(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && ascii_all(value, |byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-'
        })
}

/// `^[a-z0-9][a-z0-9.-]{0,63}$` (hosted model ids).
fn valid_model(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && ascii_all(value, |byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'.' || byte == b'-'
        })
}

/// `^[a-z]{1,16}$` (reasoning effort).
fn valid_effort(value: &str) -> bool {
    (1..=16).contains(&value.len()) && ascii_all(value, |byte| byte.is_ascii_lowercase())
}

/// `^[A-Za-z0-9_-]{1,128}$`.
fn valid_word(value: &str) -> bool {
    (1..=128).contains(&value.len())
        && ascii_all(value, |byte| {
            byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'
        })
}

/// `^run_[A-Za-z0-9_-]{1,128}$`.
pub fn valid_run_id(value: &str) -> bool {
    value.strip_prefix("run_").is_some_and(valid_word)
}

/// `^[a-z_]{1,32}$` (agent activity kind).
fn valid_kind(value: &str) -> bool {
    (1..=32).contains(&value.len())
        && ascii_all(value, |byte| byte.is_ascii_lowercase() || byte == b'_')
}

/// `^Agent(?: [0-9]{1,3})?$`.
fn valid_agent(value: &str) -> bool {
    match value.strip_prefix("Agent") {
        Some("") => true,
        Some(rest) => rest.strip_prefix(' ').is_some_and(|digits| {
            (1..=3).contains(&digits.len()) && ascii_all(digits, |byte| byte.is_ascii_digit())
        }),
        None => false,
    }
}

/// `^-?[0-9]+\.[0-9]{2}$` (dollar strings).
fn valid_dollars(value: &str) -> bool {
    let unsigned = value.strip_prefix('-').unwrap_or(value);
    match unsigned.split_once('.') {
        Some((whole, cents)) => {
            !whole.is_empty()
                && ascii_all(whole, |byte| byte.is_ascii_digit())
                && cents.len() == 2
                && ascii_all(cents, |byte| byte.is_ascii_digit())
        }
        None => false,
    }
}

/// GitHub repository name: `^[A-Za-z0-9._-]{1,100}$`, not `.`/`..`, no `.git`
/// suffix (case-sensitive, as the service checks it).
#[allow(clippy::case_sensitive_file_extension_comparisons)]
fn valid_repo_name(value: &str) -> bool {
    (1..=100).contains(&value.len())
        && ascii_all(value, |byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
        })
        && value != "."
        && value != ".."
        && !value.ends_with(".git")
}

fn valid_branch(value: &str) -> bool {
    (1..=255).contains(&value.chars().count())
        && !value
            .chars()
            .any(|character| is_control(character) || character.is_whitespace())
        && !value.contains("..")
}

/// `--wait`: `<digits>(s|m|h)`, at least 1 s and at most the manifest maximum.
fn parse_wait(value: Option<&str>) -> Result<u64, RunnerError> {
    let stream = &super::manifest()["transportClasses"]["stream"];
    let maximum = stream["maxWaitMs"].as_u64().unwrap_or(21_600_000);
    let Some(value) = value else {
        return Ok(stream["defaultWaitMs"].as_u64().unwrap_or(1_800_000));
    };
    let error = || {
        invalid(format!(
            "--wait {value_quoted} must be a duration like 90s, 10m or 2h, from 1s to {}h",
            maximum / 3_600_000,
            value_quoted = crate::error::quote(value)
        ))
    };
    let unit = match value.chars().last() {
        Some('s') => 1_000,
        Some('m') => 60_000,
        Some('h') => 3_600_000,
        _ => return Err(error()),
    };
    let digits = &value[..value.len() - 1];
    if digits.is_empty() || digits.len() > 9 || !ascii_all(digits, |byte| byte.is_ascii_digit()) {
        return Err(error());
    }
    let milliseconds = digits.parse::<u64>().map_err(|_| error())? * unit;
    if milliseconds == 0 || milliseconds > maximum {
        return Err(error());
    }
    Ok(milliseconds)
}

/// [`parse_wait`] for this invocation: a bare number (`--wait 5`) is
/// corrected to seconds (`--wait 5s`).
fn wait_option(context: &Context<'_>) -> Result<u64, RunnerError> {
    let value = context.option("--wait");
    parse_wait(value).map_err(|error| {
        let fixed = value
            .filter(|value| !value.is_empty() && ascii_all(value, |byte| byte.is_ascii_digit()))
            .map(|value| format!("{value}s"))
            .filter(|fixed| parse_wait(Some(fixed)).is_ok());
        match fixed {
            Some(fixed) => context.corrected(
                error,
                &format!("Give --wait a unit, {fixed} for seconds: `{{command}}`"),
                context.argv_with_option("--wait", &fixed),
            ),
            None => error,
        }
    })
}

fn parse_after(value: Option<&str>) -> Result<u64, RunnerError> {
    let value = value.unwrap_or("0");
    if value.is_empty() || value.len() > 10 || !ascii_all(value, |byte| byte.is_ascii_digit()) {
        return Err(invalid(format!(
            "--after {value_quoted} must be a sequence number (0 to 9999999999)",
            value_quoted = crate::error::quote(value)
        )));
    }
    value.parse::<u64>().map_err(|_| {
        invalid(format!(
            "--after {value_quoted} must be a sequence number",
            value_quoted = crate::error::quote(value)
        ))
    })
}

fn require_run_id(context: &Context<'_>) -> Result<String, RunnerError> {
    super::run_records::run_id_argument(context)
}

fn session_option(context: &Context<'_>) -> Result<Option<String>, RunnerError> {
    match context.option("--session") {
        None => Ok(None),
        Some(value) if valid_session(value) => Ok(Some(value.to_owned())),
        Some(value) => Err(invalid(format!(
            "--session {value_quoted} must be a lowercase UUID",
            value_quoted = crate::error::quote(value)
        ))),
    }
}

/// A copyable follow-up command line (the shared renderer: environment and
/// machine output mode are kept).
fn command(context: &Context<'_>, words: &str) -> String {
    context.command(words)
}

/// The verb that needs a run's live session (its refusal names it).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Verb {
    Watch,
    Cancel,
    Input,
}

/// The session for `run input|cancel`: `--session`, else the journal, else
/// the run record `GET /runs/{id}` (manifest request `index`)
/// decides: an ended run is `Ok(Err(status))` (nothing to steer or cancel);
/// a live or unknown run is refused with where its live session is.
fn resolve_session(
    context: &mut Context<'_>,
    run_id: &str,
    verb: Verb,
    index: usize,
) -> Result<Result<String, String>, RunnerError> {
    if let Some(session) = session_option(context)? {
        return Ok(Ok(session));
    }
    if let Some(entry) = context.journal().find_run(run_id) {
        return Ok(Ok(entry.session));
    }
    let request = Request::from_manifest(
        context.operation,
        index,
        format!("/runs/{}", super::http::encode_segment(run_id)),
    )
    .class(TransportClass::Control);
    let record = match context
        .send(&request)
        .and_then(|response| response.json_object())
        .and_then(|body| project_run(&Value::Object(body), true))
    {
        Ok(record) => record,
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => {
            return Err(no_live_session(context, run_id, None, verb));
        }
        Err(error) => return Err(error.with_detail("runId", run_id)),
    };
    let status = record["status"].as_str().unwrap_or_default().to_owned();
    if ENDED_STATUSES.contains(&status.as_str()) {
        return Ok(Err(status));
    }
    Err(no_live_session(context, run_id, Some(&status), verb))
}

fn object<'a>(value: &'a Value, what: &str) -> Result<&'a Map<String, Value>, RunnerError> {
    value
        .as_object()
        .ok_or_else(|| protocol(format!("{what} is not a JSON object")))
}

// ---------------------------------------------------------------------------
// run quote

/// The public hold of a `/run/quote` body; the service's price policy
/// reference stays internal.
fn quote_fields(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let hold = &body.get("hold").cloned().unwrap_or(Value::Null);
    let hold_usd = hold["hold_usd"]
        .as_str()
        .filter(|value| valid_dollars(value))
        .ok_or_else(|| protocol("quote hold.hold_usd is missing or malformed"))?;
    let hold_cents = render::usd_cents(hold_usd)
        .ok_or_else(|| protocol("quote hold.hold_usd is missing or malformed"))?;
    let ttl = hold["ttl_seconds"]
        .as_u64()
        .ok_or_else(|| protocol("quote hold.ttl_seconds is missing or malformed"))?;
    Ok(json!({"hold_usd": hold_usd, "hold_cents": hold_cents, "ttl_seconds": ttl}))
}

/// `run quote` `holdBasis`: the hold is not a price estimate.
const HOLD_BASIS: &str =
    "flat hold, independent of program and model; a run's price is known only after it settles";

fn quote(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let requested = context.option("--environment").map(str::to_owned);
    if let Some(environment) = &requested {
        if !valid_token(environment) {
            return Err(invalid(format!(
                "--environment {environment_quoted} is not an environment id (lowercase letters, digits, _ and -)",
                environment_quoted = crate::error::quote(environment)
            )));
        }
    }
    let health = context
        .send(&Request::from_manifest(context.operation, 0, "/health"))?
        .json_object()?;
    let environments = health.get("environments").cloned().unwrap_or(Value::Null);
    let available = environments["available"]
        .as_array()
        .and_then(|items| {
            items
                .iter()
                .map(|item| item.as_str().map(str::to_owned))
                .collect::<Option<Vec<_>>>()
        })
        .ok_or_else(|| protocol("service status environments.available is missing or malformed"))?;
    let environment = match &requested {
        Some(environment) => {
            if !available.contains(environment) {
                return Err(invalid(format!(
                    "environment {environment_quoted} is not offered by this service (available: {}); retry with --environment {}",
                    available.join(", "),
                    available.first().map_or("builtin", String::as_str),
                    environment_quoted = crate::error::quote(environment)
                )));
            }
            environment.clone()
        }
        None => environments["default"]
            .as_str()
            .filter(|value| render::valid_text(value, 64))
            .ok_or_else(|| protocol("service status environments.default is missing or malformed"))?
            .to_owned(),
    };
    let mut request = Request::from_manifest(context.operation, 1, "/run/quote");
    if let Some(environment) = &requested {
        request = request.query("environment", environment.clone());
    }
    let body = context.send(&request)?.json_object()?;
    let hold = quote_fields(&body)?;
    let note = match body.get("note") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(note)) => clean_line(note, 512),
        Some(_) => return Err(protocol("quote note is malformed")),
    };
    let mut text = format!("Environment: {}\n", human_safe_scalar(&environment));
    let _ = writeln!(
        text,
        "Hold: ${}, set aside from the wallet while a run is live; not its price. The same for every program and model; released within {} s when unused",
        human_safe_scalar(hold["hold_usd"].as_str().unwrap_or_default()),
        hold["ttl_seconds"]
    );
    let _ = writeln!(
        text,
        "Price: known only after a run settles; read it with `{}`",
        command(context, "run show RUN_ID")
    );
    if !note.is_empty() {
        let _ = writeln!(text, "Note: {}", human_safe_scalar(&note));
    }
    context.human = Some(text);
    Ok(json!({
        "environment": environment,
        "hold": hold,
        "holdBasis": HOLD_BASIS,
        "note": note,
    }))
}

// ---------------------------------------------------------------------------
// Event projection (decision 5) and terminal mapping (decision 9).

/// Sanitized `unrecognized` name: ASCII lowercase, other characters `_`.
fn unrecognized_name(value: &str) -> String {
    let name = value
        .chars()
        .take(64)
        .map(|character| match character {
            'a'..='z' | '0'..='9' | '_' => character,
            'A'..='Z' => character.to_ascii_lowercase(),
            _ => '_',
        })
        .collect::<String>();
    if name.is_empty() {
        "unknown".to_owned()
    } else {
        name
    }
}

fn copy_string(target: &mut Map<String, Value>, source: &Value, key: &str, max: usize, text: bool) {
    if let Some(value) = source[key].as_str() {
        let value = if text {
            clean_text(value, max)
        } else {
            clean_line(value, max)
        };
        target.insert(key.to_owned(), Value::String(value));
    }
}

fn project_status(data: &Value) -> Option<(String, Value)> {
    let status = data["status"].as_str()?;
    let mut out = Map::new();
    if status == "history_truncated" {
        copy_string(&mut out, data, "message", 1000, false);
        return Some(("history_truncated".into(), Value::Object(out)));
    }
    out.insert("status".into(), json!(clean_line(status, 32)));
    if let Some(message) = data["message"].as_str() {
        out.insert(
            "message".into(),
            json!(render::status_message(&clean_line(message, 1000))),
        );
    }
    if let (Some(steer), Some(stop)) = (
        data["controls"]["steer"].as_bool(),
        data["controls"]["stop"].as_bool(),
    ) {
        out.insert("controls".into(), json!({"steer": steer, "stop": stop}));
    }
    Some(("status".into(), Value::Object(out)))
}

fn project_activity(data: &Value) -> Option<Value> {
    let kind = data["kind"].as_str().filter(|kind| valid_kind(kind))?;
    let mut out = Map::new();
    out.insert("kind".into(), json!(kind));
    copy_string(&mut out, data, "message", 1000, true);
    copy_string(&mut out, data, "target", 500, false);
    copy_string(&mut out, data, "tool", 128, false);
    // The service's tool call reference stays internal; `input_id` names the
    // caller's own instruction.
    if let Some(value) = data["input_id"].as_str().filter(|value| valid_word(value)) {
        out.insert("input_id".into(), json!(value));
    }
    if let Some(agent) = data["agent"].as_str().filter(|agent| valid_agent(agent)) {
        out.insert("agent".into(), json!(agent));
    }
    if let Some(turn) = data["turn"]
        .as_u64()
        .filter(|turn| (1..=512).contains(turn))
    {
        out.insert("turn".into(), json!(turn));
    }
    if let Some(outcome) = data["outcome"]
        .as_str()
        .filter(|outcome| matches!(*outcome, "success" | "error" | "cancelled"))
    {
        out.insert("outcome".into(), json!(outcome));
    }
    if let Some(details) = data["details"].as_array() {
        let projected = details
            .iter()
            .filter_map(|detail| {
                let label = detail["label"].as_str()?;
                let text = detail["text"].as_str()?;
                let format = detail["format"]
                    .as_str()
                    .filter(|format| matches!(*format, "text" | "diff"))?;
                let mut item = Map::new();
                item.insert("label".into(), json!(clean_line(label, 80)));
                item.insert("text".into(), json!(clean_text(text, 4000)));
                item.insert("format".into(), json!(format));
                if detail["truncated"] == true {
                    item.insert("truncated".into(), json!(true));
                }
                Some(Value::Object(item))
            })
            .take(3)
            .collect::<Vec<_>>();
        if !projected.is_empty() {
            out.insert("details".into(), Value::Array(projected));
        }
    }
    Some(Value::Object(out))
}

fn project_browser(data: &Value) -> Option<Value> {
    let status = data["status"]
        .as_str()
        .filter(|status| matches!(*status, "live" | "ended"))?;
    let started = data["started_at"].as_str()?;
    let mut out = Map::new();
    out.insert("status".into(), json!(status));
    out.insert("started_at".into(), json!(clean_line(started, 40)));
    copy_string(&mut out, data, "ended_at", 40, false);
    Some(Value::Object(out))
}

/// Projects one non-terminal event: `(type, data)`.
fn project_event(kind: &str, data: &Value) -> (String, Value) {
    let unrecognized = || {
        (
            "unrecognized".to_owned(),
            json!({"name": unrecognized_name(kind)}),
        )
    };
    match kind {
        "status" => project_status(data).unwrap_or_else(unrecognized),
        "agent_activity" => project_activity(data)
            .map_or_else(unrecognized, |value| ("agent_activity".to_owned(), value)),
        "text_chunk" => data["text"].as_str().map_or_else(unrecognized, |text| {
            (
                "text_chunk".to_owned(),
                json!({"text": clean_text(text, 1 << 20)}),
            )
        }),
        "browser_live_view_changed" => project_browser(data).map_or_else(unrecognized, |value| {
            ("browser_live_view_changed".to_owned(), value)
        }),
        "error" => (
            "error".to_owned(),
            json!({"message": data["message"].as_str().map_or_else(
                || "The run reported an error.".to_owned(),
                |message| render::run_error_text(&clean_line(message, 4096)),
            )}),
        ),
        _ => unrecognized(),
    }
}

fn valid_commit_output(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    let keys = [
        "type",
        "provider",
        "repository",
        "branch",
        "source_commit_sha",
        "commit_sha",
        "changed_files",
        "url",
    ];
    let sha = |key: &str| {
        value[key].as_str().is_some_and(|sha| {
            sha.len() == 40 && ascii_all(sha, |byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        })
    };
    object.len() == keys.len()
        && keys.iter().all(|key| object.contains_key(*key))
        && value["type"] == "commit"
        && value["provider"] == "github"
        && value["repository"]
            .as_str()
            .is_some_and(|text| render::valid_text(text, 200))
        && value["branch"]
            .as_str()
            .is_some_and(|text| render::valid_text(text, 256))
        && sha("source_commit_sha")
        && sha("commit_sha")
        && value["changed_files"].as_array().is_some_and(|files| {
            files.len() <= 10_000
                && files
                    .iter()
                    .all(|file| file.as_str().is_some_and(super::fs::valid_relative_path))
        })
        && value["url"].as_str().is_some_and(|url| {
            url.len() <= 512
                && url.starts_with("https://github.com/")
                && url.len() > "https://github.com/".len()
                && !url.chars().any(char::is_whitespace)
        })
}

/// The closed `runTerminal` projection of a `run_complete` payload.
fn project_terminal(data: &Value) -> Result<Value, RunnerError> {
    let run_id = data["run_id"]
        .as_str()
        .filter(|run| valid_run_id(run))
        .ok_or_else(|| protocol("run_complete has no valid run_id"))?;
    let status = data["status"]
        .as_str()
        .ok_or_else(|| protocol("run_complete has no status"))?;
    let files = match &data["files"] {
        Value::Array(items) => items
            .iter()
            .filter_map(Value::as_str)
            .filter(|path| super::fs::valid_relative_path(path))
            .map(|path| json!(path))
            .collect::<Vec<_>>(),
        Value::Object(map) => {
            let mut keys = map
                .keys()
                .filter(|path| super::fs::valid_relative_path(path))
                .cloned()
                .collect::<Vec<_>>();
            keys.sort();
            keys.into_iter().map(Value::String).collect()
        }
        _ => Vec::new(),
    };
    let mut out = Map::new();
    out.insert("run_id".into(), json!(run_id));
    out.insert("status".into(), json!(clean_line(status, 32)));
    out.insert("cancelled".into(), json!(data["cancelled"] == true));
    out.insert(
        "files".into(),
        Value::Array(files.into_iter().take(10_000).collect()),
    );
    // The public environment id (`builtin`, `linux`) only: the service's
    // runtime name, runtime contract and version are not part of the record.
    if let Some(id) = data["environment"]["id"].as_str() {
        out.insert("environment".into(), json!(clean_line(id, 64)));
    }
    match &data["response"] {
        Value::String(text) => {
            out.insert("response".into(), json!(clean_text(text, 1 << 20)));
        }
        Value::Null if data.get("response").is_some() => {
            out.insert("response".into(), Value::Null);
        }
        _ => {}
    }
    for key in ["price_cents", "environment_price_cents"] {
        if let Some(value) = data[key].as_i64() {
            out.insert(key.into(), json!(value));
        }
    }
    if let Some(billing) = data["billing_status"].as_str() {
        out.insert(
            "billing_status".into(),
            json!(render::public_billing(&clean_line(billing, 32))),
        );
    }
    if let (Some(input), Some(output)) = (
        data["usage"]["input_tokens"].as_u64(),
        data["usage"]["output_tokens"].as_u64(),
    ) {
        out.insert(
            "usage".into(),
            json!({"input_tokens": input, "output_tokens": output}),
        );
    }
    if valid_commit_output(&data["output"]) {
        out.insert("output".into(), data["output"].clone());
    }
    if let Some(error) = data["error"].as_str() {
        out.insert(
            "error".into(),
            json!(render::run_error_text(&clean_line(error, 4096))),
        );
    }
    Ok(Value::Object(out))
}

/// Deadline for `--wait`. With the real transport a helper thread cancels
/// the stream promptly (heartbeats keep a quiet stream open); with a fixture
/// the virtual clock is read after each delivered event.
struct Deadline {
    start_ms: u64,
    wait_ms: u64,
    hit: Arc<AtomicBool>,
    done: Arc<AtomicBool>,
    /// Whether the run id is known. When the deadline passes before it is,
    /// the stream keeps being read until the run id arrives, for at most the
    /// stream connect timeout, so the deadline names the run (exit 21)
    /// instead of leaving the submission ambiguous (mirrors Bun).
    known: Arc<AtomicBool>,
}

/// How long a passed `--wait` still waits for the run id: the stream
/// connect timeout.
fn grace_ms() -> u64 {
    super::manifest()["transportClasses"]["stream"]["connectTimeoutMs"]
        .as_u64()
        .unwrap_or(30_000)
}

impl Deadline {
    fn start(context: &mut Context<'_>, wait_ms: u64, known: bool) -> Self {
        let deadline = Self {
            start_ms: context.monotonic_ms(),
            wait_ms,
            hit: Arc::new(AtomicBool::new(false)),
            done: Arc::new(AtomicBool::new(false)),
            known: Arc::new(AtomicBool::new(known)),
        };
        if !context.transport.is_fixture() {
            let (hit, done, known) = (
                deadline.hit.clone(),
                deadline.done.clone(),
                deadline.known.clone(),
            );
            let cancellation = context.cancellation.clone();
            let until = Instant::now() + Duration::from_millis(wait_ms);
            let grace_until = until + Duration::from_millis(grace_ms());
            std::thread::spawn(move || {
                while !done.load(Ordering::Acquire) {
                    let now = Instant::now();
                    if now >= until {
                        hit.store(true, Ordering::Release);
                        if known.load(Ordering::Acquire) || now >= grace_until {
                            cancellation.cancel();
                            return;
                        }
                    }
                    let next = if now >= until { grace_until } else { until };
                    std::thread::sleep(
                        next.saturating_duration_since(now)
                            .min(Duration::from_millis(100)),
                    );
                }
            });
        }
        deadline
    }

    /// The run id is now known: a deadline that already passed ends the
    /// wait now (the helper thread cancels within its polling interval).
    fn mark_known(&self) {
        self.known.store(true, Ordering::Release);
    }

    /// Reads the clock once; true when the deadline has passed.
    fn passed(&self, context: &mut Context<'_>) -> bool {
        if self.hit.load(Ordering::Acquire) {
            return true;
        }
        let now = context.monotonic_ms();
        if now.saturating_sub(self.start_ms) >= self.wait_ms {
            self.hit.store(true, Ordering::Release);
            return true;
        }
        false
    }

    fn was_hit(&self) -> bool {
        self.hit.load(Ordering::Acquire)
    }
}

impl Drop for Deadline {
    fn drop(&mut self) {
        self.done.store(true, Ordering::Release);
    }
}

/// Stream state shared by `run submit` and `run watch`.
struct Follow {
    run_id: Option<String>,
    session: String,
    /// The last delivered sequence (or `--after`); events at or below it are
    /// never delivered again.
    after: u64,
    /// Events delivered in this invocation.
    delivered: u64,
    /// The last `error` event's message.
    error: Option<String>,
    /// Human mode: whether the last text chunk ended with a line feed.
    text_open: bool,
    /// The last status event's status word, if any.
    status: Option<String>,
}

enum Outcome {
    Terminal(Value),
    /// End of stream after an `error` event, without `run_complete`.
    ErrorEnd,
    /// End of stream without `run_complete`; `Closed` means a clean end.
    Ended(StreamEnd),
    Detached,
}

impl Follow {
    fn details(&self, error: RunnerError) -> RunnerError {
        let mut error = error.with_detail("afterSequence", self.after);
        error = error.with_detail("session", self.session.clone());
        if let Some(run) = &self.run_id {
            error = error.with_detail("runId", run.clone());
        }
        error
    }
}

/// The copyable command that resumes following the run: exactly
/// `details.resumeArgv`, session included.
fn watch_command(context: &Context<'_>, follow: &Follow) -> String {
    match &follow.run_id {
        Some(run) => argv_command(&resume_argv(context, run, follow)),
        None => command(context, "run list --limit 5"),
    }
}

/// The copyable command that cancels the run: exactly `details.cancelArgv`.
fn cancel_command(context: &Context<'_>, run: &str, follow: &Follow) -> String {
    argv_command(&cancel_argv(context, run, follow))
}

/// A follow-up argv as a copyable command line.
fn argv_command(argv: &Value) -> String {
    let words = argv
        .as_array()
        .map(|words| {
            words
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    render::argv_text(&words)
}

/// The argv (after the product name) that resumes following the run.
fn resume_argv(context: &Context<'_>, run: &str, follow: &Follow) -> Value {
    let after = follow.after.to_string();
    json!(context.follow_up_argv(&[
        "run",
        "watch",
        run,
        "--after",
        &after,
        "--session",
        &follow.session,
    ]))
}

/// The argv (after the product name) that cancels the run.
fn cancel_argv(context: &Context<'_>, run: &str, follow: &Follow) -> Value {
    json!(context.follow_up_argv(&["run", "cancel", run, "--session", &follow.session, "--yes",]))
}

/// Adds the structured resume and cancel commands when the run id is known.
fn with_follow_up(context: &Context<'_>, follow: &Follow, error: RunnerError) -> RunnerError {
    let error = follow.details(error);
    match &follow.run_id {
        Some(run) => error
            .with_detail("resumable", true)
            .with_detail("resumeArgv", resume_argv(context, run, follow))
            .with_detail("cancelArgv", cancel_argv(context, run, follow)),
        None => error,
    }
}

/// The client stopped following a run whose id is known: never
/// retryable, because retrying `run submit` would start a second paid run.
fn detached_error(context: &Context<'_>, follow: &Follow, reason: String) -> RunnerError {
    with_follow_up(
        context,
        follow,
        RunnerError::catalog(ErrorCode::HostedRunDetached).with_detail("reason", reason),
    )
}

fn interrupted(context: &Context<'_>, follow: &Follow) -> RunnerError {
    match &follow.run_id {
        Some(run) => detached_error(
            context,
            follow,
            format!(
                "interrupted; the run continues and was not cancelled. Follow it with `{}` or cancel it with `{}`",
                watch_command(context, follow),
                cancel_command(context, run, follow)
            ),
        ),
        None => ambiguous(
            context,
            follow,
            "interrupted before the run id was known",
            None,
        ),
    }
}

fn deadline_error(context: &Context<'_>, follow: &Follow) -> RunnerError {
    if follow.run_id.is_none() {
        return ambiguous(
            context,
            follow,
            "the --wait deadline passed before the run id was known",
            None,
        );
    }
    with_follow_up(
        context,
        follow,
        RunnerError::catalog(ErrorCode::ServiceWatchDeadline).with_detail(
            "reason",
            format!(
                "the --wait deadline passed; the run continues. Resume with `{}`",
                watch_command(context, follow)
            ),
        ),
    )
}

fn stream_lost(context: &Context<'_>, follow: &Follow, what: &str) -> RunnerError {
    if follow.run_id.is_none() {
        return ambiguous(context, follow, what, None);
    }
    detached_error(
        context,
        follow,
        format!(
            "{what}; the run was not cancelled. Resume with `{}` (it reports the outcome even if the run already finished), or read the record with `{}`",
            watch_command(context, follow),
            command(
                context,
                &format!("run show {}", follow.run_id.as_deref().unwrap_or("RUN_ID"))
            )
        ),
    )
}

fn write_out(context: &mut Context<'_>, text: &str) {
    let _ = context.out.write_all(text.as_bytes());
    let _ = context.out.flush();
}

fn write_err(context: &mut Context<'_>, text: &str) {
    let _ = context.err.write_all(text.as_bytes());
    let _ = context.err.flush();
}

/// Delivers one projected event in the selected output mode.
fn deliver(
    context: &mut Context<'_>,
    follow: &mut Follow,
    kind: &str,
    data: &Value,
    sequence: u64,
    at: Option<&str>,
) {
    match context.mode {
        OutputMode::Jsonl => {
            let line = render::event_line(
                follow.run_id.as_deref(),
                Some(sequence),
                at,
                kind,
                data.clone(),
            );
            write_out(context, &format!("{line}\n"));
        }
        OutputMode::Human => {
            let line = |value: &Value| human_safe_scalar(value.as_str().unwrap_or_default());
            let text = match kind {
                "text_chunk" => {
                    let raw = data["text"].as_str().unwrap_or_default();
                    let text =
                        terminal_text(&assistant_words(raw).unwrap_or_else(|| raw.to_owned()));
                    if !text.is_empty() {
                        follow.text_open = !text.ends_with('\n');
                        write_out(context, &text);
                    }
                    return;
                }
                // The status word only: the service's status message names
                // its runtime, which is not the person's business (JSONL
                // keeps the event data).
                "status" => format!("[status] {}\n", line(&data["status"])),
                "agent_activity" => {
                    let message = data
                        .get("message")
                        .map(|message| {
                            format!(": {}", agent_message(message.as_str().unwrap_or_default()))
                        })
                        .unwrap_or_default();
                    let target = data
                        .get("target")
                        .map(|target| format!(" ({})", line(target)))
                        .unwrap_or_default();
                    format!("[agent] {}{message}{target}\n", line(&data["kind"]))
                }
                "history_truncated" => format!(
                    "[history truncated] {}\n",
                    data.get("message").map(line).unwrap_or_default()
                ),
                "browser_live_view_changed" => {
                    format!("[browser] {}\n", line(&data["status"]))
                }
                "error" => format!("[error] {}\n", line(&data["message"])),
                _ => return,
            };
            write_err(context, &text);
        }
        OutputMode::Json => {}
    }
}

/// The words of an agent's structured final answer (`{status, reason,
/// semantic_diff: {summary, notes}}`), which human output shows instead of
/// the JSON: the reason, the summary when it differs, then each note, one per
/// line. `None` for any other text, which prints as the run wrote it.
fn assistant_words(text: &str) -> Option<String> {
    let trimmed = text.trim();
    if !trimmed.starts_with('{') {
        return None;
    }
    let value: Value = serde_json::from_str(trimmed).ok()?;
    let status = value["status"].as_str()?;
    let reason = value["reason"].as_str()?;
    let diff = value.get("semantic_diff")?;
    let summary = diff["summary"].as_str().unwrap_or_default();
    let mut lines = Vec::new();
    for line in [reason, summary] {
        if !line.trim().is_empty() && !lines.contains(&line) {
            lines.push(line);
        }
    }
    for note in diff["notes"].as_array().map_or(&[][..], Vec::as_slice) {
        if let Some(note) = note.as_str().filter(|note| !note.trim().is_empty()) {
            lines.push(note);
        }
    }
    if lines.is_empty() {
        lines.push(status);
    }
    Some(lines.join("\n") + "\n")
}

/// A human `[agent]` message: its line breaks stay line breaks, each
/// continuation line indented under the tag; other controls are escaped.
fn agent_message(message: &str) -> String {
    crate::error::human_safe_multiline(message.replace("\r\n", "\n").trim_end_matches('\n'))
        .replace('\n', "\n    ")
}

/// Records the run id (first time) and the last sequence in the journal
/// entry for this session, if there is one.
fn record(context: &Context<'_>, follow: &Follow, create: Option<&Entry>) {
    let journal = context.journal();
    let entry = match journal.get(&follow.session) {
        Ok(Some(entry)) => Some(entry),
        _ => create.cloned(),
    };
    if let Some(mut entry) = entry {
        if entry.run_id.is_some() && entry.run_id != follow.run_id {
            return;
        }
        entry.run_id.clone_from(&follow.run_id);
        entry.last_sequence = entry.last_sequence.max(follow.after);
        let _ = journal.write(&entry);
    }
}

/// Consumes one event stream until a terminal event, the end of the stream,
/// a detach, the deadline or an interrupt.
fn consume(
    context: &mut Context<'_>,
    reader: &mut SseReader,
    follow: &mut Follow,
    detach: bool,
    deadline: &Deadline,
) -> Result<Outcome, RunnerError> {
    loop {
        let item = match reader.next_item() {
            Ok(item) => item,
            Err(error) if error.code == ErrorCode::Cancelled => {
                return Err(if deadline.was_hit() {
                    deadline_error(context, follow)
                } else {
                    interrupted(context, follow)
                });
            }
            Err(error) => return Err(follow.details(error)),
        };
        let event = match item {
            SseItem::End(end) => {
                return Ok(if follow.error.is_some() && end == StreamEnd::Closed {
                    Outcome::ErrorEnd
                } else {
                    Outcome::Ended(end)
                });
            }
            SseItem::Event(event) => event,
        };
        let data: Value = serde_json::from_str(&event.data)
            .map_err(|_| follow.details(protocol("an event's data is not JSON")))?;
        if !data.is_object() {
            return Err(follow.details(protocol("an event's data is not a JSON object")));
        }
        let kind = if event.event == "message" {
            data["type"].as_str().unwrap_or("message").to_owned()
        } else {
            event.event.clone()
        };
        let sequence = data["sequence"]
            .as_u64()
            .filter(|sequence| *sequence <= MAX_SAFE_INTEGER)
            .or_else(|| {
                event
                    .id
                    .as_deref()
                    .filter(|id| {
                        (1..=15).contains(&id.len()) && ascii_all(id, |byte| byte.is_ascii_digit())
                    })
                    .and_then(|id| id.parse::<u64>().ok())
            })
            .ok_or_else(|| {
                follow.details(protocol(format!(
                    "a {} event has no sequence",
                    unrecognized_name(&kind)
                )))
            })?;
        // A replay from 0 (see `watch`) honors the terminal event even at or
        // below --after; every other event there was already delivered.
        if sequence <= follow.after && kind != "run_complete" {
            continue;
        }
        if follow.run_id.is_none() {
            let run = data["run_id"]
                .as_str()
                .filter(|run| valid_run_id(run))
                .ok_or_else(|| follow.details(protocol("the first event has no valid run_id")))?;
            follow.run_id = Some(run.to_owned());
            context.run_id = Some(run.to_owned());
            deadline.mark_known();
            record(context, follow, None);
        }
        let at = data["at"].as_str().map(|at| clean_line(at, 40));
        if kind == "run_complete" {
            follow.after = follow.after.max(sequence);
            return Ok(Outcome::Terminal(data));
        }
        let (projected_kind, projected) = project_event(&kind, &data);
        follow.after = sequence;
        follow.delivered += 1;
        if projected_kind == "error" {
            follow.error = projected["message"].as_str().map(str::to_owned);
        }
        if projected_kind == "status" {
            if let Some(status) = projected["status"].as_str() {
                follow.status = Some(status.to_owned());
            }
        }
        deliver(
            context,
            follow,
            &projected_kind,
            &projected,
            sequence,
            at.as_deref(),
        );
        if detach {
            return Ok(Outcome::Detached);
        }
        if deadline.passed(context) {
            return Err(deadline_error(context, follow));
        }
    }
}

/// Maps a terminal `run_complete` to the stream result or its error.
fn finish_terminal(
    context: &mut Context<'_>,
    follow: &Follow,
    data: &Value,
) -> Result<Value, RunnerError> {
    let run = project_terminal(data).map_err(|error| follow.details(error))?;
    if context.mode == OutputMode::Human && follow.text_open {
        write_out(context, "\n");
    }
    if run["cancelled"] == true {
        return Err(follow.details(
            RunnerError::catalog(ErrorCode::HostedRunCancelled).with_detail(
                "reason",
                format!(
                    "the run was cancelled on the service; read it with `{}`",
                    command(
                        context,
                        &format!("run show {}", follow.run_id.as_deref().unwrap_or("RUN_ID"))
                    )
                ),
            ),
        ));
    }
    if run["status"] != "completed" {
        let error = run["error"]
            .as_str()
            .map(|error| format!(": {}", clean_line(error, 480)))
            .unwrap_or_default();
        return Err(follow.details(RunnerError::hosted_run_failed(format!(
            "the run finished with status {}{error}",
            run["status"].as_str().unwrap_or_default()
        ))));
    }
    if context.mode == OutputMode::Human {
        let price = run["price_cents"]
            .as_i64()
            .map(|cents| format!(" Price: {}.", super::render::dollars(cents)))
            .unwrap_or_default();
        let files = run["files"].as_array().map_or(0, Vec::len);
        let summary = format!(
            "Run {} completed.{price} Files: {files}.\n",
            human_safe_scalar(run["run_id"].as_str().unwrap_or_default())
        );
        write_err(context, &summary);
        context.human = Some(String::new());
    }
    Ok(json!({
        "runId": follow.run_id,
        "run_id": follow.run_id,
        "session": follow.session,
        "detached": false,
        "afterSequence": follow.after,
        "run": run,
    }))
}

/// `run submit --session S` when this machine's journal already maps S to
/// a run: that run (`reused: true`), never a second submission.
fn reused_result(context: &mut Context<'_>, follow: &Follow, from_journal: bool) -> Value {
    let mut result = detached_result(context, follow, "unknown");
    result["reused"] = json!(true);
    if context.mode == OutputMode::Human {
        let run = follow.run_id.as_deref().unwrap_or_default();
        let place = if from_journal {
            ", submitted earlier from this machine"
        } else {
            ""
        };
        context.human = Some(format!(
            "Session {} already has run {}{place}; nothing was submitted again.\nFollow it with: {}\nRead it with: {}\n",
            human_safe_scalar(&follow.session),
            human_safe_scalar(run),
            watch_command(context, follow),
            command(context, &format!("run show {run}"))
        ));
    }
    result
}

/// A detached (or reused) run: `run` is `{run_id, status}`, the status the
/// stream last reported, else `fallback` (`running` after a detach,
/// `unknown` for a reused session).
fn detached_result(context: &mut Context<'_>, follow: &Follow, fallback: &str) -> Value {
    if context.mode == OutputMode::Human {
        if follow.text_open {
            write_out(context, "\n");
        }
        let run = follow.run_id.as_deref().unwrap_or_default();
        context.human = Some(format!(
            "Run {} is running (detached after sequence {}).\nFollow it with: {}\nCancel it with: {}\n",
            human_safe_scalar(run),
            follow.after,
            watch_command(context, follow),
            cancel_command(context, run, follow)
        ));
    }
    let run = follow.run_id.as_ref().map_or(Value::Null, |run| {
        json!({
            "run_id": run,
            "status": follow.status.as_deref().unwrap_or(fallback),
        })
    });
    let mut result = json!({
        "runId": follow.run_id,
        "run_id": follow.run_id,
        "session": follow.session,
        "detached": true,
        "afterSequence": follow.after,
        "run": run,
    });
    // The commands that follow or cancel the detached run, as in the
    // exit-21 errors' details.
    if let Some(run) = &follow.run_id {
        result["resumeArgv"] = resume_argv(context, run, follow);
        result["cancelArgv"] = cancel_argv(context, run, follow);
    }
    result
}

// ---------------------------------------------------------------------------
// run submit

struct Repository {
    owner: String,
    name: String,
    branch: Option<String>,
}

impl Repository {
    fn url(&self) -> String {
        format!("https://github.com/{}/{}", self.owner, self.name)
    }

    fn same(&self, other: &Self) -> bool {
        self.owner.eq_ignore_ascii_case(&other.owner) && self.name.eq_ignore_ascii_case(&other.name)
    }
}

fn parse_repository(option: &str, value: &str) -> Result<Repository, RunnerError> {
    let error = || {
        invalid(format!(
            "{option} {value_quoted} must be OWNER/NAME or OWNER/NAME@BRANCH (a GitHub repository)",
            value_quoted = crate::error::quote(value)
        ))
    };
    let (name, branch) = match value.split_once('@') {
        Some((name, branch)) => (name, Some(branch)),
        None => (value, None),
    };
    let (owner, name) = name.split_once('/').ok_or_else(error)?;
    if !program_ref::valid_owner(owner)
        || !valid_repo_name(name)
        || branch.is_some_and(|branch| !valid_branch(branch))
    {
        return Err(error());
    }
    Ok(Repository {
        owner: owner.to_owned(),
        name: name.to_owned(),
        branch: branch.map(str::to_owned),
    })
}

fn valid_input_key(key: &str) -> bool {
    (1..=128).contains(&key.chars().count()) && !key.chars().any(is_control)
}

fn parse_inputs(context: &Context<'_>) -> Result<BTreeMap<String, String>, RunnerError> {
    let cwd = context.system.current_dir.clone();
    let mut inputs = BTreeMap::new();
    if let Some(file) = context.option("--inputs-file") {
        let text = super::fs::read_text(&cwd, file, MAX_INPUT_FILE_BYTES, "--inputs-file")?;
        let value: Value = serde_json::from_str(&text).map_err(|_| {
            invalid(format!(
                "--inputs-file {file_quoted} is not valid JSON",
                file_quoted = crate::error::quote(file)
            ))
        })?;
        let object = value.as_object().ok_or_else(|| {
            invalid(format!(
                "--inputs-file {file_quoted} must hold a JSON object of string values",
                file_quoted = crate::error::quote(file)
            ))
        })?;
        for (key, value) in object {
            let Some(value) = value.as_str() else {
                return Err(invalid(format!(
                    "--inputs-file {file_quoted}: input {key_quoted} must be a string",
                    file_quoted = crate::error::quote(file),
                    key_quoted = crate::error::quote(key)
                )));
            };
            if !valid_input_key(key) {
                return Err(invalid(format!(
                    "--inputs-file {file_quoted}: input name {key_quoted} must be 1 to 128 characters without control characters",
                    file_quoted = crate::error::quote(file),
                    key_quoted = crate::error::quote(key)
                )));
            }
            inputs.insert(key.clone(), value.to_owned());
        }
    }
    let mut seen = BTreeSet::new();
    for raw in context.invocation.option_values("--input") {
        let Some((key, value)) = raw.split_once('=') else {
            return Err(invalid(format!(
                "--input {raw_quoted} must be KEY=VALUE or KEY=@FILE",
                raw_quoted = crate::error::quote(raw)
            )));
        };
        if !valid_input_key(key) {
            return Err(invalid(format!(
                "--input name {key_quoted} must be 1 to 128 characters without control characters",
                key_quoted = crate::error::quote(key)
            )));
        }
        if !seen.insert(key.to_owned()) {
            return Err(invalid(format!("--input {key} was given more than once")));
        }
        let value = match value.strip_prefix('@') {
            Some("") => {
                return Err(invalid(format!(
                    "--input {key}=@ needs a file path after @"
                )));
            }
            Some(path) => super::fs::read_text(
                &cwd,
                path,
                MAX_INPUT_FILE_BYTES,
                &format!("--input {key} file"),
            )?,
            None => value.to_owned(),
        };
        inputs.insert(key.to_owned(), value);
    }
    Ok(inputs)
}

fn looks_like_url(value: &str) -> bool {
    value.split_once("://").is_some_and(|(scheme, _)| {
        !scheme.is_empty()
            && scheme.as_bytes()[0].is_ascii_alphabetic()
            && ascii_all(scheme, |byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'.' | b'-')
            })
    })
}

struct Submission {
    body: Vec<u8>,
    /// Query parameters after `live` and `session`.
    extra_query: Vec<(String, String)>,
    source_sha256: Option<String>,
    session: Option<String>,
    wait_ms: u64,
    environment: Option<String>,
}

fn prepare_submission(context: &mut Context<'_>) -> Result<Submission, RunnerError> {
    let file = context.argument("FILE").map(str::to_owned);
    let from = context.option("--from").map(str::to_owned);
    match (&file, &from) {
        (Some(_), Some(_)) => {
            return Err(invalid(
                "give either FILE or --from OWNER/SLUG[@REV], not both",
            ));
        }
        (None, None) => {
            return Err(invalid(
                "missing program: give FILE, - for standard input, or --from OWNER/SLUG[@REV]",
            ));
        }
        _ => {}
    }
    if let Some(file) = file.as_deref().filter(|file| looks_like_url(file)) {
        return Err(invalid(format!(
            "URL sources are not supported ({file_quoted}); save the program to a file or run a saved program with --from OWNER/SLUG[@REV]",
            file_quoted = crate::error::quote(file)
        )));
    }
    let reference = from
        .as_deref()
        .map(|value| program_ref::parse_own_allowed_with(value, true))
        .transpose()?;
    let session = session_option(context)?;
    let wait_ms = wait_option(context)?;
    let model = context.option("--model").map(str::to_owned);
    if let Some(model) = model.as_deref().filter(|model| !valid_model(model)) {
        return Err(invalid(format!(
            "--model {model_quoted} is not a model id; list them with `{}`",
            command(context, "model list"),
            model_quoted = crate::error::quote(model)
        )));
    }
    let effort = context.option("--reasoning-effort").map(str::to_owned);
    if let Some(effort) = effort.as_deref().filter(|effort| !valid_effort(effort)) {
        return Err(invalid(format!(
            "--reasoning-effort {effort_quoted} must be lowercase letters (for example low, medium or high)",
            effort_quoted = crate::error::quote(effort)
        )));
    }
    let environment = context.option("--environment").map(str::to_owned);
    let runtime = context.option("--runtime").map(str::to_owned);
    for (option, kind, value) in [
        ("--environment", "an environment", &environment),
        ("--runtime", "a runtime", &runtime),
    ] {
        if let Some(value) = value.as_deref().filter(|value| !valid_token(value)) {
            return Err(invalid(format!(
                "{option} {value_quoted} is not {kind} id (lowercase letters, digits, _ and -)",
                value_quoted = crate::error::quote(value)
            )));
        }
    }
    let mut repositories: Vec<Repository> = Vec::new();
    for value in context.invocation.option_values("--repo") {
        let repository = parse_repository("--repo", value)?;
        if repositories.iter().any(|seen| seen.same(&repository)) {
            return Err(invalid(format!(
                "--repo {value_quoted} was given more than once",
                value_quoted = crate::error::quote(value)
            )));
        }
        repositories.push(repository);
    }
    let commit = context
        .option("--commit-output")
        .map(|value| parse_repository("--commit-output", value))
        .transpose()?;
    if let Some(commit) = &commit {
        if !repositories
            .iter()
            .any(|repository| repository.same(commit))
        {
            return Err(invalid(format!(
                "--commit-output {}/{} must also be given as --repo {}/{}[@BRANCH]; the service commits only to a context repository",
                commit.owner, commit.name, commit.owner, commit.name
            )));
        }
    }
    let inputs = parse_inputs(context)?;
    let mut body = Map::new();
    let mut source_sha256 = None;
    if let Some(file) = &file {
        let cwd = context.system.current_dir.clone();
        let program_text = super::fs::read_text(&cwd, file, MAX_PROGRAM_BYTES, "program file")?;
        if program_text.trim().is_empty() {
            return Err(invalid(format!(
                "program file {file_quoted} is empty",
                file_quoted = crate::error::quote(file)
            )));
        }
        source_sha256 = Some(format!("{:x}", Sha256::digest(program_text.as_bytes())));
        check_parameters(&program_text, &inputs)?;
        body.insert("content".into(), json!(program_text));
    }
    if !inputs.is_empty() {
        body.insert("inputs".into(), json!(inputs));
    }
    if let Some(model) = model {
        body.insert("model".into(), json!(model));
    }
    if let Some(effort) = effort {
        body.insert("reasoning_effort".into(), json!(effort));
    }
    let repository_values = repositories
        .iter()
        .map(|repository| {
            let mut value = Map::new();
            value.insert("url".into(), json!(repository.url()));
            if let Some(branch) = &repository.branch {
                value.insert("branch".into(), json!(branch));
            }
            Value::Object(value)
        })
        .collect::<Vec<_>>();
    let mut extra_query = Vec::new();
    if let Some(environment) = &environment {
        extra_query.push(("environment".to_owned(), environment.clone()));
    }
    if let Some(runtime) = &runtime {
        extra_query.push(("runtime".to_owned(), runtime.clone()));
    }
    if !repository_values.is_empty() {
        extra_query.push((
            "repositories".to_owned(),
            render::canonical(&Value::Array(repository_values.clone())),
        ));
        body.insert("repositories".into(), Value::Array(repository_values));
    }
    if let Some(commit) = &commit {
        let mut output = Map::new();
        output.insert("type".into(), json!("commit"));
        output.insert("repository".into(), json!(commit.url()));
        if let Some(branch) = &commit.branch {
            output.insert("branch".into(), json!(branch));
        }
        body.insert("output".into(), Value::Object(output));
        extra_query.push(("output_repository".to_owned(), commit.url()));
    }
    if let Some(model) = body.get("model").and_then(Value::as_str).map(str::to_owned) {
        // An unknown model fails before confirmation (request 4).
        context.check_model(4, &model)?;
    }
    // So do an environment or runtime the service does not offer (GET /health).
    check_offered(context, environment.as_deref(), runtime.as_deref())?;
    if let Some(mut reference) = reference {
        // A bare SLUG, `@N` and the caller's own pinned revisions resolve
        // through the caller's revisions (request 3); a latest reference
        // reads the newest rev_id (request 0).
        let pinned =
            program_ref::resolve_to_run(context, &mut reference, 3, 0, Some("--from"), true)?;
        body.insert("program_ref".into(), json!(pinned));
    }
    let body = render::canonical(&Value::Object(body)).into_bytes();
    if body.len() > MAX_BODY_BYTES {
        return Err(invalid(format!(
            "the submission is {} bytes, above the {MAX_BODY_BYTES}-byte limit; shrink the program or inputs",
            body.len()
        )));
    }
    Ok(Submission {
        body,
        extra_query,
        source_sha256,
        session,
        wait_ms,
        environment,
    })
}

/// One input a program declares: its name and whether its description says
/// it is optional.
struct Parameter {
    name: String,
    optional: bool,
}

/// Whether `text` contains `word` (ASCII, any case) between non-word
/// characters, like a `\bword\b` search.
fn has_word(text: &str, word: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    let bytes = lower.as_bytes();
    let is_word = |byte: u8| byte.is_ascii_alphanumeric() || byte == b'_';
    lower.match_indices(word).any(|(start, _)| {
        let end = start + word.len();
        (start == 0 || !is_word(bytes[start - 1])) && (end == bytes.len() || !is_word(bytes[end]))
    })
}

/// A markdown heading line: 1 to 6 `#` then a space or tab; the rest.
fn heading(line: &str) -> Option<&str> {
    let hashes = line.bytes().take_while(|byte| *byte == b'#').count();
    if !(1..=6).contains(&hashes) {
        return None;
    }
    let rest = &line[hashes..];
    rest.starts_with([' ', '\t']).then_some(rest)
}

/// The input names a program file declares: the `` `name` `` bullets under
/// its `Parameters` heading, up to the next heading. `None` when the program
/// has no such heading, so nothing is checked. Only the heading and bullet
/// names are read (mirrors Bun `declaredParameters`).
fn declared_parameters(text: &str) -> Option<Vec<Parameter>> {
    let lines = text
        .split('\n')
        .map(|line| line.strip_suffix('\r').unwrap_or(line))
        .collect::<Vec<_>>();
    let start = lines.iter().position(|line| {
        heading(line).is_some_and(|rest| rest.trim_matches([' ', '\t']) == "Parameters")
    })?;
    let mut parameters: Vec<Parameter> = Vec::new();
    for line in &lines[start + 1..] {
        if heading(line).is_some() {
            break;
        }
        let Some(item) = line
            .trim_start_matches([' ', '\t'])
            .strip_prefix(['-', '*'])
        else {
            continue;
        };
        if !item.starts_with([' ', '\t']) {
            continue;
        }
        let Some(item) = item.trim_start_matches([' ', '\t']).strip_prefix('`') else {
            continue;
        };
        let Some(end) = item.find('`').filter(|end| *end > 0) else {
            continue;
        };
        let name = &item[..end];
        if !parameters.iter().any(|known| known.name == name) {
            parameters.push(Parameter {
                name: name.to_owned(),
                optional: has_word(&item[end + 1..], "optional"),
            });
        }
    }
    Some(parameters)
}

/// A user-supplied value quoted for a reason (the shared quote rules).
fn quoted(value: &str) -> String {
    crate::error::quote(value)
}

/// Compares the inputs with the program's declared Parameters before any
/// confirmation or request: an input the program does not declare, or a
/// declared input that is neither given nor optional, is `INVOCATION_INVALID`
/// (mirrors Bun `checkParameters`).
fn check_parameters(text: &str, inputs: &BTreeMap<String, String>) -> Result<(), RunnerError> {
    let Some(declared) = declared_parameters(text) else {
        return Ok(());
    };
    let names = declared
        .iter()
        .map(|parameter| parameter.name.as_str())
        .collect::<Vec<_>>();
    let listed = if names.is_empty() {
        "none".to_owned()
    } else {
        names.join(", ")
    };
    if let Some(key) = inputs.keys().find(|key| !names.contains(&key.as_str())) {
        let near = super::did_you_mean(key, names.iter().copied())
            .map(|near| format!("; did you mean {}?", quoted(near)))
            .unwrap_or_default();
        return Err(invalid(format!(
            "input {} is not a parameter of this program (its parameters: {listed}){near}",
            quoted(key)
        )));
    }
    let missing = declared
        .iter()
        .filter(|parameter| !parameter.optional && !inputs.contains_key(&parameter.name))
        .map(|parameter| parameter.name.as_str())
        .collect::<Vec<_>>();
    if missing.is_empty() {
        return Ok(());
    }
    Err(invalid(format!(
        "the program needs input{} {}; pass {}",
        if missing.len() == 1 { "" } else { "s" },
        missing
            .iter()
            .map(|name| quoted(name))
            .collect::<Vec<_>>()
            .join(", "),
        missing
            .iter()
            .map(|name| format!("--input {name}=VALUE"))
            .collect::<Vec<_>>()
            .join(" ")
    )))
}

/// Refuses an `--environment` (or `--runtime`) that /health does not list,
/// before any confirmation, as `run quote` does. Advisory: when /health
/// cannot be read or lists none, the service's own check applies; only an
/// interrupt stops here (mirrors Bun `checkOffered`).
fn check_offered(
    context: &mut Context<'_>,
    environment: Option<&str>,
    runtime: Option<&str>,
) -> Result<(), RunnerError> {
    if environment.is_none() && runtime.is_none() {
        return Ok(());
    }
    let index = context.operation["requests"]
        .as_array()
        .and_then(|requests| {
            requests
                .iter()
                .position(|request| request["method"] == "GET" && request["path"] == "/health")
        })
        .unwrap_or_default();
    let request =
        Request::from_manifest(context.operation, index, "/health").class(TransportClass::Control);
    let health = match context
        .send(&request)
        .and_then(|response| response.json_object())
    {
        Ok(health) => Value::Object(health),
        Err(error) if error.code == ErrorCode::Cancelled => return Err(error),
        Err(_) => return Ok(()),
    };
    for (option, key, value) in [
        ("--environment", "environments", environment),
        ("--runtime", "runtimes", runtime),
    ] {
        let Some(value) = value else {
            continue;
        };
        let Some(list) = health[key]["available"].as_array() else {
            continue;
        };
        let available = list
            .iter()
            .filter_map(Value::as_str)
            .filter(|item| valid_token(item))
            .collect::<Vec<_>>();
        if available.is_empty() || available.contains(&value) {
            continue;
        }
        let (article, noun) = if option == "--environment" {
            ("an", "environment")
        } else {
            ("a", "runtime")
        };
        let error = invalid(format!(
            "{noun} {} is not offered by this service (available: {}); retry with {option} {}",
            quoted(value),
            available.join(", "),
            available[0]
        ));
        return Err(context.corrected(
            error,
            &format!("Rerun with {article} {noun} the service offers: `{{command}}`"),
            context.argv_with_option(option, available[0]),
        ));
    }
    Ok(())
}

fn submit_query(session: &str, extra: &[(String, String)]) -> Vec<(String, String)> {
    let mut query = vec![
        ("live".to_owned(), "1".to_owned()),
        ("session".to_owned(), session.to_owned()),
    ];
    query.extend(extra.iter().cloned());
    query
}

/// The service's duplicate-session refusal text (`POST /run`). The
/// deployed service sends it without a `code` and without naming the run.
const DUPLICATE_SESSION: &str = "This session already has a run.";

/// Whether a 409 is the duplicate-session refusal rather than another
/// conflict such as `model_policy_mismatch` or an admission refusal
///: it names a run, or it carries the duplicate text and no `code`.
fn duplicate_session(response: &Response) -> bool {
    if recovered_run_id(response).is_some() {
        return true;
    }
    let Ok(body) = serde_json::from_slice::<Value>(&response.body) else {
        return false;
    };
    body.get("code").is_none()
        && body["error"]
            .as_str()
            .is_some_and(|error| error.starts_with(DUPLICATE_SESSION))
}

/// The run id a 409 live-session response names, if any.
fn recovered_run_id(response: &Response) -> Option<String> {
    if let Some(run) = response.header("x-run-id").filter(|run| valid_run_id(run)) {
        return Some(run.to_owned());
    }
    serde_json::from_slice::<Value>(&response.body)
        .ok()?
        .get("run_id")?
        .as_str()
        .filter(|run| valid_run_id(run))
        .map(str::to_owned)
}

fn ambiguous(
    context: &Context<'_>,
    follow: &Follow,
    reason: &str,
    status: Option<u16>,
) -> RunnerError {
    let resume = session_resubmit_argv(context, &follow.session);
    let mut error = RunnerError::catalog(ErrorCode::RunSubmissionAmbiguous)
        .with_detail("session", follow.session.clone())
        .with_detail(
            "reason",
            format!(
                "{reason}; the submission was not repeated. Check `{}` before submitting again, or recover the run with `{}`: the same session never starts a second run; the local run journal keeps the session",
                command(context, "run list --limit 5"),
                render::argv_text(&resume)
            ),
        );
    if let Some(status) = status {
        error = error.with_detail("serviceStatus", status);
    }
    error
        .with_detail(
            "suggestedArgv",
            json!(context.follow_up_argv(&["run", "list", "--limit", "5"])),
        )
        .with_detail("resumeArgv", json!(resume))
}

/// This `run submit` again with `--session S --detach`: the service answers
/// a session that already has a run with that run (`result.reused`), so it
/// recovers an ambiguous submission without starting a second run (mirrors
/// Bun `sessionResubmitArgv`).
fn session_resubmit_argv(context: &Context<'_>, session: &str) -> Vec<String> {
    let mut argv = context.argv_setting_option("--session", session);
    if !context.flag("--detach") {
        let end = argv
            .iter()
            .position(|token| token == "--")
            .unwrap_or(argv.len());
        argv.insert(end, "--detach".to_owned());
    }
    argv
}

fn submit(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let submission = prepare_submission(context)?;
    if context.invocation.preview || !context.invocation.yes {
        let placeholder = submission.session.as_deref().unwrap_or("{session}");
        let query = submit_query(placeholder, &submission.extra_query);
        let mut planned = context.planned(2, "/run", &query, Some(&submission.body));
        let mut quote_request = Request::from_manifest(context.operation, 1, "/run/quote")
            .class(TransportClass::Control);
        if let Some(environment) = &submission.environment {
            quote_request = quote_request.query("environment", environment.clone());
        }
        // The quote is advisory: a failed quote never hides the plan.
        if let Ok(Ok(body)) = context
            .send(&quote_request)
            .map(|response| response.json_object())
        {
            if let Ok(hold) = quote_fields(&body) {
                planned["quote"] = json!({"hold": hold});
            }
        }
        if let Gate::Preview(result) = context.gate(planned)? {
            return Ok(result);
        }
    }
    let session_given = submission.session.is_some();
    let session = match submission.session.clone() {
        Some(session) => session,
        None => context.uuid_v4()?,
    };
    let created_at = context.now_rfc3339();
    let journal = context.journal();
    journal.prune(&created_at, PRUNE_DAYS);
    let entry = Entry {
        session: session.clone(),
        run_id: None,
        created_at,
        last_sequence: 0,
        source_sha256: submission.source_sha256.clone(),
    };
    // `--session` of a run this machine already submitted: nothing is sent
    // again (a second submission would be refused, or start a second paid
    // run); the result is that run, with the commands that follow it.
    if session_given {
        if let Some(known) = journal.get(&session).ok().flatten() {
            if let Some(run) = known.run_id.clone() {
                let follow = Follow {
                    run_id: Some(run),
                    session: session.clone(),
                    after: known.last_sequence,
                    delivered: 0,
                    error: None,
                    text_open: false,
                    status: None,
                };
                return Ok(reused_result(context, &follow, true));
            }
        }
    }
    if !(session_given && matches!(journal.get(&session), Ok(Some(_)))) {
        journal.write(&entry)?;
    }
    let mut request = Request::from_manifest(context.operation, 2, "/run")
        .header("X-Session-Id", session.clone())
        .header("Accept", "text/event-stream");
    request.query = submit_query(&session, &submission.extra_query);
    request.body = Some(submission.body.clone());
    let mut follow = Follow {
        run_id: None,
        session,
        after: 0,
        delivered: 0,
        error: None,
        text_open: false,
        status: None,
    };
    let deadline = Deadline::start(context, submission.wait_ms, false);
    let detach = context.flag("--detach");
    let mut resubmitted = false;
    loop {
        let open = match context.open_stream(&request) {
            Ok(open) => open,
            Err(error) if error.code == ErrorCode::Cancelled => {
                return Err(if deadline.was_hit() {
                    deadline_error(context, &follow)
                } else {
                    interrupted(context, &follow)
                });
            }
            Err(error) => return Err(error),
        };
        let outcome = match open {
            StreamOpen::Dropped => {
                if resubmitted {
                    return Err(ambiguous(
                        context,
                        &follow,
                        "the connection failed twice before the service answered",
                        None,
                    ));
                }
                resubmitted = true;
                continue;
            }
            StreamOpen::Response(response) => {
                let duplicate = response.status == 409 && duplicate_session(&response);
                if response.status == 409 && resubmitted && !duplicate {
                    // The resubmission was refused for another reason; the
                    // first attempt's fate is still unknown.
                    return Err(ambiguous(
                        context,
                        &follow,
                        "the resubmission after a lost connection was refused with a conflict",
                        Some(409),
                    ));
                }
                if duplicate && (resubmitted || session_given) {
                    let Some(run) = recovered_run_id(&response) else {
                        return Err(ambiguous(
                            context,
                            &follow,
                            "the service reports that this session already has a run but did not name it",
                            Some(409),
                        ));
                    };
                    follow.run_id = Some(run.clone());
                    context.run_id = Some(run.clone());
                    deadline.mark_known();
                    record(context, &follow, Some(&entry));
                    // `--session S` of a session that already has a run: that
                    // run (`reused: true`), as when this machine's journal
                    // names it.
                    if session_given && !resubmitted {
                        return Ok(reused_result(context, &follow, false));
                    }
                    if detach {
                        return Ok(detached_result(context, &follow, "running"));
                    }
                    return Err(stream_lost(
                        context,
                        &follow,
                        &format!(
                            "the submission was admitted as {run} but its event stream was lost"
                        ),
                    ));
                }
                return Err(context.classify(&request, &response));
            }
            StreamOpen::Events {
                headers,
                mut reader,
                ..
            } => {
                if follow.run_id.is_none() {
                    if let Some(run) = headers.get("x-run-id").filter(|run| valid_run_id(run)) {
                        follow.run_id = Some(run.clone());
                        context.run_id = Some(run.clone());
                        deadline.mark_known();
                        record(context, &follow, Some(&entry));
                    }
                }
                let result = consume(context, &mut reader, &mut follow, detach, &deadline);
                record(context, &follow, None);
                result?
            }
        };
        match outcome {
            Outcome::Terminal(data) => return finish_terminal(context, &follow, &data),
            Outcome::Detached => return Ok(detached_result(context, &follow, "running")),
            Outcome::ErrorEnd => return Err(error_end(&follow)),
            Outcome::Ended(_) if follow.run_id.is_none() && follow.delivered == 0 => {
                if resubmitted {
                    return Err(ambiguous(
                        context,
                        &follow,
                        "the event stream ended twice before the first event",
                        None,
                    ));
                }
                resubmitted = true;
            }
            Outcome::Ended(end) => {
                return Err(stream_lost(
                    context,
                    &follow,
                    if end == StreamEnd::Closed {
                        "the event stream closed before run_complete"
                    } else {
                        "the event stream was lost before run_complete"
                    },
                ));
            }
        }
    }
}

fn error_end(follow: &Follow) -> RunnerError {
    follow.details(RunnerError::hosted_run_failed(format!(
        "the event stream ended after an error: {}",
        clean_line(follow.error.as_deref().unwrap_or_default(), 480)
    )))
}

// ---------------------------------------------------------------------------
// run watch

fn watch(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    if context.argument("RUN_ID") != Some(LATEST_RUN) {
        require_run_id(context)?;
    }
    let after = parse_after(context.option("--after"))?;
    let wait_ms = wait_option(context)?;
    session_option(context)?;
    let run_id = run_argument(context)?;
    let session = match session_option(context)? {
        Some(session) => Some(session),
        None => context
            .journal()
            .find_run(&run_id)
            .map(|entry| entry.session),
    };
    context.run_id = Some(run_id.clone());
    let Some(session) = session else {
        return watch_record(context, &run_id);
    };
    let mut request = Request::from_manifest(
        context.operation,
        0,
        format!("/run/{}/events", super::http::encode_segment(&run_id)),
    )
    .header("Accept", "text/event-stream");
    request.query = vec![
        ("after".to_owned(), after.to_string()),
        ("session".to_owned(), session.clone()),
    ];
    let mut follow = Follow {
        run_id: Some(run_id),
        session,
        after,
        delivered: 0,
        error: None,
        text_open: false,
        status: None,
    };
    let deadline = Deadline::start(context, wait_ms, true);
    // The service keeps a live run's stream open (heartbeats) and closes it
    // cleanly only once the run is terminal. A clean close without a
    // `run_complete` past --after therefore means the terminal event is at
    // or below --after: replay once from 0 to report the run's real outcome
    // instead of claiming it continues.
    let mut replayed = false;
    loop {
        let open = match context.open_stream(&request) {
            Ok(open) => open,
            Err(error) if error.code == ErrorCode::Cancelled => {
                return Err(if deadline.was_hit() {
                    deadline_error(context, &follow)
                } else {
                    interrupted(context, &follow)
                });
            }
            Err(error) => return Err(follow.details(error)),
        };
        let mut reader = match open {
            StreamOpen::Dropped => {
                return Err(stream_lost(
                    context,
                    &follow,
                    "the connection failed before the service answered",
                ));
            }
            StreamOpen::Response(response) => return Err(context.classify(&request, &response)),
            StreamOpen::Events { reader, .. } => reader,
        };
        let result = consume(context, &mut reader, &mut follow, false, &deadline);
        record(context, &follow, None);
        match result? {
            Outcome::Terminal(data) => return finish_terminal(context, &follow, &data),
            Outcome::ErrorEnd => return Err(error_end(&follow)),
            Outcome::Ended(StreamEnd::Closed) if !replayed && after > 0 => {
                replayed = true;
                "0".clone_into(&mut request.query[0].1);
            }
            Outcome::Ended(StreamEnd::Closed) => {
                return Err(follow.details(protocol(format!(
                    "the event stream closed without run_complete; read the run with `{}`",
                    command(
                        context,
                        &format!("run show {}", follow.run_id.as_deref().unwrap_or("RUN_ID"))
                    )
                ))));
            }
            Outcome::Ended(_) | Outcome::Detached => {
                return Err(stream_lost(
                    context,
                    &follow,
                    "the event stream was lost before run_complete",
                ));
            }
        }
    }
}

/// `run watch` without a live session on this machine: the
/// stream cannot be opened, so read the run record instead. An ended run
/// reports its outcome like a replay would (exit 0, 22 or 24) from the record;
/// a run without an ended record is still live elsewhere (or unknown), and the
/// error says where its session is.
fn watch_record(context: &mut Context<'_>, run_id: &str) -> Result<Value, RunnerError> {
    let request = Request::from_manifest(
        context.operation,
        1,
        format!("/runs/{}", super::http::encode_segment(run_id)),
    )
    .class(TransportClass::Control);
    let record = match context
        .send(&request)
        .and_then(|response| response.json_object())
        .and_then(|body| project_run(&Value::Object(body), true))
    {
        Ok(record) => record,
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => {
            return Err(run_not_found(context, run_id, error));
        }
        Err(error) => return Err(error),
    };
    let status = record["status"].as_str().unwrap_or_default().to_owned();
    if !ENDED_STATUSES.contains(&status.as_str()) {
        return Err(no_live_session(context, run_id, Some(&status), Verb::Watch));
    }
    let run = record_terminal(&record);
    let files: Vec<String> = run["files"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    let source = "from its record; this machine has no live session to replay";
    let detailed = |error: RunnerError| {
        error
            .with_detail("runId", run_id)
            .with_detail("files", files.clone())
            .with_detail("source", "record")
    };
    if run["cancelled"] == true {
        return Err(detailed(
            RunnerError::catalog(ErrorCode::HostedRunCancelled).with_detail(
                "reason",
                format!(
                    "the run was cancelled on the service ({source}); read it with `{}`",
                    command(context, &format!("run show {run_id}"))
                ),
            ),
        ));
    }
    if status != "completed" {
        let error = run["error"]
            .as_str()
            .map(|error| format!(": {}", clean_line(error, 480)))
            .unwrap_or_default();
        let read = if files.is_empty() {
            String::new()
        } else {
            format!(
                "; its {} file{}: `{}`",
                files.len(),
                if files.len() == 1 { "" } else { "s" },
                command(context, &format!("run download {run_id}"))
            )
        };
        return Err(detailed(RunnerError::hosted_run_failed(format!(
            "the run finished with status {status}{error} ({source}){read}"
        ))));
    }
    if context.mode == OutputMode::Human {
        let price = run["price_cents"]
            .as_i64()
            .map(|cents| format!(" Price: {}.", super::render::dollars(cents)))
            .unwrap_or_default();
        let mut text = format!(
            "Run {} completed ({source}).{price} Files: {}.\n",
            human_safe_scalar(run_id),
            files.len()
        );
        for file in &files {
            let _ = writeln!(text, "  {}", human_safe_scalar(file));
        }
        if let Some(first) = files.first() {
            let _ = writeln!(
                text,
                "Read a file with: {}",
                command(context, &format!("run show {run_id} --file {first}"))
            );
        }
        context.human = Some(text);
    }
    Ok(json!({
        "runId": run_id,
        "run_id": run_id,
        "session": Value::Null,
        "detached": false,
        "afterSequence": 0,
        "run": run,
        "source": "record",
    }))
}

/// `run watch` of a run the service has no record of and this machine did
/// not submit: `SERVICE_RESOURCE_NOT_FOUND` naming the run, unless the typed
/// id is a mistyped copy of one this machine submitted (mirrors Bun
/// `runNotFound`).
fn run_not_found(context: &Context<'_>, run_id: &str, error: RunnerError) -> RunnerError {
    if journal_near_run(context, run_id).is_some() {
        return no_live_session(context, run_id, None, Verb::Watch);
    }
    let error = error.with_detail("runId", run_id).with_detail(
        "reason",
        format!(
            "run {run_id} was not found: no run has this id, or it has not ended and was submitted from another machine (a run's record is written when it ends)"
        ),
    );
    super::not_found::explain_not_found(
        error,
        context.mode,
        "run",
        run_id,
        Some(&["run", "list", "--limit", "5"]),
    )
}

/// The closed `runTerminal` projection of an ended run record (no response
/// text; the record has none). A run its owner stopped is cancelled.
fn record_terminal(record: &Value) -> Value {
    let status = record["status"].as_str().unwrap_or_default();
    let mut out = Map::new();
    out.insert("run_id".to_owned(), record["run_id"].clone());
    out.insert("status".to_owned(), json!(status));
    out.insert(
        "cancelled".to_owned(),
        json!(status == "cancelled" || status == "canceled" || stopped_by_owner(&record["error"])),
    );
    out.insert(
        "files".to_owned(),
        record.get("files").cloned().unwrap_or_else(|| json!([])),
    );
    for key in [
        "environment",
        "price_cents",
        "environment_price_cents",
        "billing_status",
        "usage",
        "error",
    ] {
        if let Some(value) = record.get(key) {
            out.insert(key.to_owned(), value.clone());
        }
    }
    Value::Object(out)
}

/// The one run id in this machine's run journal that `typed` truncates
/// (a prefix) or misspells (at most two edits), if exactly one does.
fn journal_near_run(context: &Context<'_>, typed: &str) -> Option<String> {
    let mut found = context
        .journal()
        .entries()
        .into_iter()
        .filter_map(|entry| entry.run_id)
        .filter(|run| {
            run != typed
                && ((run.starts_with(typed) && typed.len() > "run_".len())
                    || super::did_you_mean(typed, [run.as_str()]).is_some())
        })
        .collect::<Vec<_>>();
    found.sort();
    found.dedup();
    match found.as_slice() {
        [only] => Some(only.clone()),
        _ => None,
    }
}

/// `run watch|cancel|input` of a run with no live session here and no ended
/// record: the Action names where the session is for that verb.
fn no_live_session(
    context: &Context<'_>,
    run_id: &str,
    status: Option<&str>,
    verb: Verb,
) -> RunnerError {
    // A run with no record that this machine did not submit, while it did
    // submit a run whose id the typed one truncates or misspells: that run.
    if status.is_none() {
        if let Some(near) = journal_near_run(context, run_id) {
            let error = invalid(format!(
                "{run_id} has no record and this machine did not submit it; this machine submitted {near}, which the id looks like a truncated or mistyped copy of"
            ))
            .with_detail("runId", run_id);
            return context.corrected(
                error,
                &format!("Use the run id {near}: `{{command}}`"),
                context.argv_with_argument(run_id, &near),
            );
        }
    }
    let state = status.map_or_else(
        || "has no ended record yet: it is still running (a run's record is written when it ends) or it does not exist".to_owned(),
        |status| format!("is {}", clean_line(status, 32)),
    );
    let (there, action) = match verb {
        Verb::Watch => (
            "watch it there",
            "Watch the run from the machine that submitted it or pass --session UUID; once it ends, read its outcome with `{command}`.",
        ),
        Verb::Cancel => (
            "cancel it there",
            "Cancel the run from the machine that submitted it (its run journal holds the live session) or pass that session with --session UUID; nothing was cancelled. Once it ends, read its outcome with `{command}`.",
        ),
        Verb::Input => (
            "send the instruction there",
            "Send the instruction from the machine that submitted the run (its run journal holds the live session) or pass that session with --session UUID; nothing was sent. Once it ends, read its outcome with `{command}`.",
        ),
    };
    let reason = format!(
        "{run_id} {state}, and this machine's run journal has no live session for it. A run's live session is kept in the run journal of the machine that submitted it: {there}, or pass --session UUID; once the run ends, `{}` reads its outcome",
        command(context, &format!("run show {run_id}"))
    );
    with_next_command(
        context,
        invalid(reason).with_detail("runId", run_id),
        action,
        &["run", "show", run_id],
    )
}

// ---------------------------------------------------------------------------
// run input

/// The service's refusal to steer a run that has ended (or is stopping).
const INPUT_ENDED: &str = "This run is no longer accepting instructions.";
/// The service's refusal to cancel a run that has ended.
const CANCEL_ENDED: &str = "This run has already ended.";

fn input(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let run_id = require_run_id(context)?;
    let text = context.argument("TEXT").unwrap_or_default().to_owned();
    let units = text.encode_utf16().count();
    if text.trim().is_empty() || units > MAX_INSTRUCTION_UNITS {
        return Err(invalid(format!(
            "instruction text must be 1 to {MAX_INSTRUCTION_UNITS} characters and not blank"
        )));
    }
    if text
        .chars()
        .any(|character| is_control(character) && !matches!(character, '\t' | '\n' | '\r'))
    {
        return Err(invalid(
            "instruction text must not contain control characters other than tab and line breaks",
        ));
    }
    let id = match context.option("--id") {
        Some(id) if valid_session(id) => Some(id.to_owned()),
        Some(id) => {
            return Err(invalid(format!(
                "--id {id_quoted} must be a lowercase UUID",
                id_quoted = crate::error::quote(id)
            )));
        }
        None => None,
    };
    let session = match resolve_session(context, &run_id, Verb::Input, 1)? {
        Ok(session) => session,
        // An ended run read from its record: the same
        // non-retryable conflict as the service's 409, before any write.
        Err(status) => {
            return Err(with_next_command(
                context,
                RunnerError::catalog(ErrorCode::ServiceWriteConflict)
                    .with_detail(
                        "reason",
                        format!(
                            "the run has ended (status {}) and no longer accepts instructions; nothing was sent",
                            clean_line(&status, 32)
                        ),
                    )
                    .with_detail("runId", run_id.clone())
                    .with_detail("source", "record"),
                "Do not retry; the run takes no more instructions. Read its outcome with `{command}`.",
                &["run", "show", &run_id],
            ));
        }
    };
    let id = match id {
        Some(id) => id,
        None => context.uuid_v4()?,
    };
    let path = format!("/run/{}/input", super::http::encode_segment(&run_id));
    let query = vec![("session".to_owned(), session)];
    let body = render::canonical(&json!({"id": id, "text": text})).into_bytes();
    let planned = context.planned(0, &path, &query, Some(&body));
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let mut request = Request::from_manifest(context.operation, 0, path);
    request.query = query;
    request.body = Some(body);
    let response = match context.send(&request) {
        // Steering an ended run can never succeed; retrying is pointless.
        Err(error) if conflict_saying(&error, INPUT_ENDED) => {
            return Err(with_next_command(
                context,
                error
                    .with_detail(
                        "reason",
                        "the run has ended (or is being cancelled) and no longer accepts instructions",
                    )
                    .with_detail("runId", run_id.clone()),
                "Do not retry; the run takes no more instructions. Read its outcome with `{command}`.",
                &["run", "show", &run_id],
            ));
        }
        other => other?.json_object()?,
    };
    let status = response.get("status").and_then(Value::as_str);
    if status != Some("queued") || response.get("id").and_then(Value::as_str) != Some(id.as_str()) {
        return Err(protocol(
            "the instruction response is not {id, status: queued}",
        ));
    }
    if context.mode == OutputMode::Human {
        context.human = Some(format!(
            "Instruction {id} queued for {}.\n",
            human_safe_scalar(&run_id)
        ));
    }
    Ok(json!({"runId": run_id, "id": id, "status": "queued"}))
}

// ---------------------------------------------------------------------------
// run cancel

fn project_balance(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let balance = object(
        body.get("balance").unwrap_or(&Value::Null),
        "wallet balance",
    )?;
    let mut out = Map::new();
    for prefix in ["available", "posted", "reserved"] {
        let cents = balance
            .get(&format!("{prefix}_cents"))
            .and_then(Value::as_i64)
            .ok_or_else(|| protocol(format!("wallet balance {prefix}_cents is missing")))?;
        let dollars = balance
            .get(&format!("{prefix}_dollars"))
            .and_then(Value::as_str)
            .filter(|value| valid_dollars(value))
            .ok_or_else(|| protocol(format!("wallet balance {prefix}_dollars is missing")))?;
        out.insert(format!("{prefix}_cents"), json!(cents));
        out.insert(format!("{prefix}_dollars"), json!(dollars));
    }
    Ok(Value::Object(out))
}

fn cancel(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let run_id = require_run_id(context)?;
    let session = match resolve_session(context, &run_id, Verb::Cancel, 2)? {
        Ok(session) => session,
        // An ended run read from its record: already done.
        Err(status) => return Ok(ended_result(context, &run_id, Some(&status))),
    };
    let path = format!("/run/{}/cancel", super::http::encode_segment(&run_id));
    let query = vec![("session".to_owned(), session)];
    let planned = context.planned(0, &path, &query, None);
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let mut request = Request::from_manifest(context.operation, 0, path);
    request.query = query;
    let response = match context.send(&request) {
        // Cancelling an ended run is already done: report it as success.
        Err(error) if conflict_saying(&error, CANCEL_ENDED) => {
            return already_ended(context, &run_id);
        }
        other => other?.json_object()?,
    };
    let status = response
        .get("status")
        .and_then(Value::as_str)
        .map(|status| clean_line(status, 32))
        .ok_or_else(|| protocol("the cancel response has no status"))?;
    let balance = context
        .send(&Request::from_manifest(context.operation, 1, "/wallet/balance"))
        .and_then(|response| response.json_object())
        .and_then(|body| project_balance(&body))
        .map_err(|error| {
            error.with_detail("runId", run_id.clone()).with_detail(
                "reason",
                format!(
                    "the cancel was accepted (status {status}) but the wallet balance could not be read; check it with `{}`",
                    command(context, "wallet balance")
                ),
            )
        })?;
    if context.mode == OutputMode::Human {
        context.human = Some(format!(
            "Cancel requested for {} (status: {}).\nWallet: available ${}, reserved ${}, balance ${}.\nA reserved hold stays until the service settles the run. Watch the run finish with: {}\n",
            human_safe_scalar(&run_id),
            human_safe_scalar(&status),
            balance["available_dollars"].as_str().unwrap_or_default(),
            balance["reserved_dollars"].as_str().unwrap_or_default(),
            balance["posted_dollars"].as_str().unwrap_or_default(),
            command(context, &format!("run watch {run_id}"))
        ));
    }
    Ok(json!({"runId": run_id, "status": status, "balance": balance}))
}

/// `run cancel` of a run that has already ended: exit 0 with
/// `{status: "already_ended", runStatus}`. `runStatus` is the run record's
/// status, or null while the record is not written yet (404). The wallet is
/// not read.
fn already_ended(context: &mut Context<'_>, run_id: &str) -> Result<Value, RunnerError> {
    let request = Request::from_manifest(
        context.operation,
        2,
        format!("/runs/{}", super::http::encode_segment(run_id)),
    )
    .class(TransportClass::Control);
    let run_status = match context
        .send(&request)
        .and_then(|response| response.json_object())
        .and_then(|body| project_run(&Value::Object(body), true))
    {
        Ok(record) => super::run_records::ended_status(&record),
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => None,
        Err(error) => {
            return Err(error.with_detail("runId", run_id).with_detail(
                "reason",
                format!(
                    "the run has already ended (nothing was cancelled) but its record could not be read; read it with `{}`",
                    command(context, &format!("run show {run_id}"))
                ),
            ));
        }
    };
    Ok(ended_result(context, run_id, run_status.as_deref()))
}

/// The `already_ended` result of `run cancel` (exit 0) for a run record
/// status, or null while the record is not written yet.
fn ended_result(context: &mut Context<'_>, run_id: &str, run_status: Option<&str>) -> Value {
    if context.mode == OutputMode::Human {
        let state = run_status.map_or_else(
            || "its record is not written yet".to_owned(),
            |status| format!("status: {}", human_safe_scalar(status)),
        );
        context.human = Some(format!(
            "Run {} has already ended ({state}); nothing was cancelled.\nRead it with: {}\n",
            human_safe_scalar(run_id),
            command(context, &format!("run show {run_id}"))
        ));
    }
    json!({"runId": run_id, "status": "already_ended", "runStatus": run_status})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validators_match_the_schema_patterns() {
        assert!(valid_run_id("run_abc-DEF_1"));
        assert!(!valid_run_id("run_") && !valid_run_id("abc") && !valid_run_id("run_a b"));
        assert!(
            valid_token("builtin")
                && valid_token("linux-2")
                && !valid_token("Linux")
                && !valid_token("-a")
        );
        assert!(valid_model("model-luna") && !valid_model("GPT") && !valid_model(""));
        assert!(
            valid_agent("Agent")
                && valid_agent("Agent 12")
                && !valid_agent("Agent ")
                && !valid_agent("Agent 1234")
        );
        assert!(
            valid_dollars("1.02")
                && valid_dollars("-0.50")
                && !valid_dollars("1.2")
                && !valid_dollars(".20")
        );
        assert!(
            valid_repo_name("repo.name") && !valid_repo_name("x.git") && !valid_repo_name("..")
        );
    }

    #[test]
    fn wait_and_after_are_bounded() {
        assert_eq!(parse_wait(None).unwrap(), 1_800_000);
        assert_eq!(parse_wait(Some("90s")).unwrap(), 90_000);
        assert_eq!(parse_wait(Some("6h")).unwrap(), 21_600_000);
        for bad in ["7h", "0s", "10", "1d", "s", "-1m", "1.5h"] {
            assert!(parse_wait(Some(bad)).is_err(), "{bad}");
        }
        assert_eq!(parse_after(Some("42")).unwrap(), 42);
        assert!(parse_after(Some("-1")).is_err() && parse_after(Some("12345678901")).is_err());
    }

    #[test]
    fn sanitation_and_unrecognized_names() {
        assert_eq!(clean_line("a\nb\u{7f}c", 10), "a b c");
        assert_eq!(clean_text("a\nb\u{1b}c", 10), "a\nb c");
        assert_eq!(clean_line("héllo", 2), "hé");
        assert_eq!(unrecognized_name("Commit-Done"), "commit_done");
        assert_eq!(unrecognized_name(""), "unknown");
        assert_eq!(terminal_text("x\u{9b}y"), "x y");
    }

    #[test]
    fn terminal_projection_drops_signed_urls_and_normalizes_files() {
        let run = project_terminal(&json!({
            "run_id": "run_1", "status": "completed", "files": {"b.txt": "x", "a.txt": "y", "../bad": ""},
            "file_urls": {"a.txt": "https://x?tok=secret"}, "price_cents": 2, "session": "s",
            "usage": {"input_tokens": 1, "output_tokens": 2}, "output": {"type": "commit"}
        }))
        .unwrap();
        assert_eq!(run["files"], json!(["a.txt", "b.txt"]));
        assert_eq!(run["cancelled"], false);
        assert!(
            run.get("file_urls").is_none()
                && run.get("output").is_none()
                && run.get("session").is_none()
        );
    }

    #[test]
    fn events_project_to_closed_shapes() {
        let (kind, data) = project_event(
            "status",
            &json!({"status": "history_truncated", "message": "gap"}),
        );
        assert_eq!(
            (kind.as_str(), data),
            ("history_truncated", json!({"message": "gap"}))
        );
        let (kind, data) = project_event(
            "agent_activity",
            &json!({"kind": "tool_start", "turn": 999, "agent": "Agent 2", "call_id": "c 1"}),
        );
        assert_eq!(
            (kind.as_str(), data),
            (
                "agent_activity",
                json!({"kind": "tool_start", "agent": "Agent 2"})
            )
        );
        let (kind, data) = project_event("browser_live_view", &json!({"url": "https://secret"}));
        assert_eq!(
            (kind.as_str(), data),
            ("unrecognized", json!({"name": "browser_live_view"}))
        );
    }
}
