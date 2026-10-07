//! Service job operations: `job list|show|create|update|
//! configure|delete|deliveries|rotate-secret` and `job contract
//! list|attach|detach`.
//!
//! - Job specs and configurations come from `--spec-file` / `--config-file`
//!   (a path or `-`): a UTF-8 JSON object of at most 64 KiB. They are checked
//!   locally (object, size, `type` on create, `interval_seconds`
//!   60..=2,678,400, no result-only camelCase names or `inputEntries` in a
//!   configuration) before any request and then sent byte for byte; the
//!   service validates the rest.
//! - Configured inputs named like /cost/i are listed in
//!   `run_configuration.input_entries` as {name, value}.
//! - Every job result is built from its public field list, in `snake_case`:
//!   `job` (not the service's trigger), `max_jobs`, `job_limit`, the opaque
//!   `revision_token`, and run counts folded into the user states
//!   `queued`, `running`, `completed`, `failed`, `cancelled` and
//!   `awaiting_billing`. Any other service field (internal references,
//!   adoption and driver detail, repository detail objects, receiver/reply
//!   details) is dropped. Every epoch-ms time `x_at` gains an additive
//!   `x_at_iso` (RFC 3339 UTC).
//! - `job configure --config-file` takes the same `snake_case` names
//!   (`revision_token`, `run_configuration.context_repositories`); the
//!   product translates them to the service's names before sending.
//! - The webhook `signing_secret` and `endpoint` appear only in the result of
//!   `job create` and `job rotate-secret`, never in stderr; human mode prints a
//!   stderr warning without the secret. Both results, and `job show` when the
//!   service's endpoint is the secret-free job-id webhook path, carry the
//!   absolute `endpoint_url` built from the environment origin.
//! - `job contract attach|detach` take a pinned `OWNER/SLUG@REV`.
//! - `job contract attach` also sets a webhook binding's execution settings.
//!   It reads the job (webhook jobs only) when a setting is given, and always
//!   reads the bound contracts first: a re-attached binding keeps its saved
//!   settings, by sending only the changes to a service that merges them
//!   (one that lists `environment`) or the merged settings in full otherwise.
//!
//! Mirrors `cli/bun/src/core/service/jobs.ts`.
use super::http::{Request, encode_segment};
use super::program_ref;
use super::render::{absolute_url, add_iso, canonical, iso_ms, next_line, valid_text};
use super::runs::{self, Repository};
use super::{Context, Environment, Gate};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::fmt::Write as _;

/// Largest job spec or configuration file.
pub const SPEC_MAX_BYTES: u64 = 65_536;
/// Schedule cadence bounds enforced by the service (`validateScheduleCadence`).
pub const INTERVAL_MIN: i64 = 60;
pub const INTERVAL_MAX: i64 = 2_678_400;
/// Largest integer both products represent exactly (2^53 - 1).
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const SECRET_WARNING: &str = "Warning: the signing secret printed on stdout is shown only once. Store it now; `cli job rotate-secret` replaces it.\n";

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.invocation.operation.as_str() {
        "job.list" => list(context),
        "job.show" => show(context),
        "job.create" => create(context),
        "job.update" => update(context, false),
        "job.configure" => update(context, true),
        "job.delete" => delete(context),
        "job.deliveries" => deliveries(context),
        "job.rotate-secret" => rotate_secret(context),
        "job.contract.list" => contract_list(context),
        "job.contract.attach" => contract_change(context, true),
        "job.contract.detach" => contract_change(context, false),
        _ => context.not_implemented(),
    }
}

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn protocol(field: &str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
        .with_detail("reason", format!("unexpected job response: {field}"))
}

// ------------------------------------------------------------------ inputs

/// The `JOB_ID` argument: 1..=256 code points, no control characters. Any
/// other shape is sent percent-encoded; the service answers 404 for unknown
/// ids.
fn job_id(context: &Context<'_>) -> Result<String, RunnerError> {
    let id = context.argument("JOB_ID").unwrap_or_default();
    if valid_text(id, 256) {
        Ok(id.to_owned())
    } else {
        Err(invalid(
            "job id must be 1 to 256 characters without control characters; list ids with `cli job list`",
        ))
    }
}

/// The public run states of a job's run counts, in display order, with the
/// service's queue states each one folds (identical in both ports): unknown
/// service states are dropped.
const RUN_STATES: [(&str, &[&str]); 6] = [
    ("queued", &["pending", "queued"]),
    ("running", &["claimed", "running"]),
    ("completed", &["completed"]),
    ("failed", &["failed", "error", "ambiguous"]),
    ("cancelled", &["cancelled", "canceled"]),
    ("awaiting_billing", &["billing_pending", "pending_funds"]),
];

/// The human label of a public run state.
fn run_state_label(state: &str) -> &str {
    if state == "awaiting_billing" {
        "awaiting billing settlement"
    } else {
        state
    }
}

/// The human summary of a job's run counts: each nonzero public state with
/// its label, in order (identical in both ports).
fn run_counts(counts: &Map<String, Value>) -> String {
    let shown = RUN_STATES
        .iter()
        .filter_map(|(state, _)| {
            let count = counts.get(*state).and_then(Value::as_u64).unwrap_or(0);
            (count > 0).then(|| format!("{} {count}", run_state_label(state)))
        })
        .collect::<Vec<_>>();
    if shown.is_empty() {
        "none yet".to_owned()
    } else {
        shown.join(", ")
    }
}

/// An exact integer; a zero-fraction float (`86400.0`) counts, as in the
/// service's `Number.isSafeInteger`.
fn as_integer(value: &Value) -> Option<i64> {
    if let Some(number) = value.as_i64() {
        return (number.unsigned_abs() <= MAX_SAFE_INTEGER).then_some(number);
    }
    let float = value.as_f64()?;
    #[allow(clippy::cast_precision_loss)]
    let exact = float.fract() == 0.0 && float.abs() <= MAX_SAFE_INTEGER as f64;
    #[allow(clippy::cast_possible_truncation)]
    exact.then_some(float as i64)
}

fn interval_reason() -> String {
    format!("interval_seconds must be an integer from {INTERVAL_MIN} to {INTERVAL_MAX}")
}

fn result_only(prefix: &str, snake: &str, camel: &str) -> String {
    format!("the request field is {prefix}{snake} (snake_case), not {camel}")
}

/// `camelCase` -> `snake_case`: the public name of a service field, and the
/// request name of a camelCase key.
fn snake_case(key: &str) -> String {
    key.chars().fold(String::new(), |mut out, character| {
        if character.is_ascii_uppercase() {
            out.push('_');
            out.push(character.to_ascii_lowercase());
        } else {
            out.push(character);
        }
        out
    })
}

/// The request name of a key: its `snake_case` form, with the service's name
/// of the opaque configuration token mapped to `revision_token` (identical
/// in both ports).
fn request_name(key: &str) -> String {
    match snake_case(key).as_str() {
        "configuration_token" => "revision_token".to_owned(),
        other => other.to_owned(),
    }
}

/// "a, b and c".
fn and_list(items: &[&str]) -> String {
    match items {
        [] => String::new(),
        [one] => (*one).to_owned(),
        [rest @ .., last] => format!("{} and {last}", rest.join(", ")),
    }
}

/// The closed-schema walk over a job spec. The schema is the
/// operation's manifest `spec`; every violation is collected, in key order.
struct SpecCheck<'c, 'a> {
    context: &'c Context<'a>,
    violations: Vec<String>,
}

impl SpecCheck<'_, '_> {
    /// Checks `object` against `{required, keys}`; `path` prefixes nested
    /// names (`bindings[0].`), `label` names the object in a missing-key
    /// violation, and `hint` follows it.
    fn object(
        &mut self,
        object: &Map<String, Value>,
        keys: &Map<String, Value>,
        required: &[&str],
        path: &str,
        label: &str,
        hint: &str,
    ) {
        let mut covered = Vec::new();
        let mut names = object.keys().collect::<Vec<_>>();
        names.sort();
        for name in names {
            let value = &object[name];
            if let Some(field) = keys.get(name) {
                self.value(value, field, &format!("{path}{name}"));
                continue;
            }
            let snake = request_name(name);
            if name == "cron" && keys.contains_key("interval_seconds") {
                self.violations.push(format!(
                    "unknown key {path}cron; a schedule job runs every {path}interval_seconds seconds (3600 = hourly, 86400 = daily), not on a cron expression"
                ));
                covered.push("interval_seconds".to_owned());
            } else if (name == "input_entries" || name == "inputEntries")
                && keys.contains_key("inputs")
            {
                self.violations.push(format!(
                    "{path}{name} appears only in results: move each {{name, value}} entry into {path}inputs as \"name\": \"value\" and remove {name} (an input left out is deleted)"
                ));
                covered.push("inputs".to_owned());
            } else if snake != *name && keys.contains_key(&snake) {
                self.violations.push(result_only(path, &snake, name));
                covered.push(snake);
            } else if let Some(near) = nearest_key(name, keys) {
                self.violations.push(format!(
                    "unknown key {path}{name}; did you mean {path}{near}?"
                ));
                covered.push(near.to_owned());
            } else if label == "a job spec" {
                // No known type to list the keys of.
                self.violations
                    .push(format!("unknown key {path}{name}; no job type accepts it"));
            } else {
                let mut accepted = keys.keys().map(String::as_str).collect::<Vec<_>>();
                accepted.sort_unstable();
                self.violations.push(format!(
                    "unknown key {path}{name} in {label} (accepted: {})",
                    accepted.join(", ")
                ));
            }
        }
        let missing = required
            .iter()
            .copied()
            .filter(|key| !object.contains_key(*key) && !covered.iter().any(|seen| seen == key))
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            let missing_names = missing
                .iter()
                .map(|key| format!("{path}{key}"))
                .collect::<Vec<_>>();
            let missing_names = missing_names.iter().map(String::as_str).collect::<Vec<_>>();
            self.violations
                .push(format!("{label} needs {}{hint}", and_list(&missing_names)));
        }
    }

    fn value(&mut self, value: &Value, field: &Value, path: &str) {
        let problem = match field {
            Value::String(kind) => match kind.as_str() {
                "text" => (!value.is_string()).then(|| format!("{path} must be a string")),
                "integer" => as_integer(value)
                    .is_none()
                    .then(|| format!("{path} must be an integer")),
                "interval" => (!as_integer(value)
                    .is_some_and(|n| (INTERVAL_MIN..=INTERVAL_MAX).contains(&n)))
                .then(|| {
                    format!("{path} must be an integer from {INTERVAL_MIN} to {INTERVAL_MAX}")
                }),
                // `job create` resolves and pins a program reference the way
                // `run submit --from` does.
                "programRef" => match value.as_str() {
                    Some(text)
                        if text.len() <= 200
                            && program_ref::parse_own_allowed_with(text, true).is_ok() =>
                    {
                        None
                    }
                    Some(text) => Some(format!(
                        "{path}: program reference {text_quoted} must be [OWNER/]SLUG[@REV]: an optional owner handle, a lowercase slug and, after @, the rev_id or a revision number of your own program; a bare SLUG is your own program",
                        text_quoted = crate::error::quote(text)
                    )),
                    None => Some(format!(
                        "{path} must be a program reference [OWNER/]SLUG[@REV]"
                    )),
                },
                "pinnedRef" => match value.as_str() {
                    Some(text) if is_program_ref(text) => None,
                    Some(text) => Some(format!("{path}: {}", unpinned_reason(self.context, text))),
                    None => Some(format!(
                        "{path} must be a pinned program reference OWNER/SLUG@REV"
                    )),
                },
                "stringMap" => match value {
                    Value::Object(map) => {
                        let mut names = map.keys().collect::<Vec<_>>();
                        names.sort();
                        for name in names {
                            if !map[name].is_string() {
                                self.violations.push(format!(
                                    "{path}.{name} must be a string (got {})",
                                    json_type(&map[name])
                                ));
                            }
                        }
                        None
                    }
                    _ => Some(format!("{path} must be an object of name -> string")),
                },
                "object" => (!value.is_object()).then(|| format!("{path} must be an object")),
                "array" => (!value.is_array()).then(|| format!("{path} must be an array")),
                _ => None,
            },
            Value::Object(shape) => {
                if let Some(Value::Array(allowed)) = shape.get("enum") {
                    let allowed = allowed.iter().filter_map(Value::as_str).collect::<Vec<_>>();
                    (!value.as_str().is_some_and(|text| allowed.contains(&text)))
                        .then(|| format!("{path} must be one of {}", allowed.join(", ")))
                } else if let Some(sub) = shape.get("object") {
                    match value {
                        Value::Object(object) => {
                            let (keys, required) = keys_and_required(sub);
                            self.object(object, &keys, &required, &format!("{path}."), path, "");
                            None
                        }
                        _ => Some(format!("{path} must be an object")),
                    }
                } else if let Some(sub) = shape.get("array") {
                    match value {
                        Value::Array(items) => {
                            let (keys, required) = keys_and_required(sub);
                            for (index, item) in items.iter().enumerate() {
                                let at = format!("{path}[{index}]");
                                match item {
                                    Value::Object(object) => self.object(
                                        object,
                                        &keys,
                                        &required,
                                        &format!("{at}."),
                                        &at,
                                        "",
                                    ),
                                    _ => self.violations.push(format!("{at} must be an object")),
                                }
                            }
                            None
                        }
                        _ => Some(format!("{path} must be an array")),
                    }
                } else {
                    None
                }
            }
            _ => None,
        };
        if let Some(problem) = problem {
            self.violations.push(problem);
        }
    }
}

/// The accepted key nearest to `name`, compared without case, `_` and `-`
/// (so `intervalSecond` finds `interval_seconds`), within the manifest's
/// did-you-mean distance.
fn nearest_key<'k>(name: &str, keys: &'k Map<String, Value>) -> Option<&'k str> {
    let fold = |text: &str| {
        text.chars()
            .filter(|character| *character != '_' && *character != '-')
            .flat_map(char::to_lowercase)
            .collect::<String>()
    };
    let target = fold(name);
    let folded = keys
        .keys()
        .map(|key| (fold(key), key.as_str()))
        .collect::<Vec<_>>();
    if let Some((_, key)) = folded.iter().find(|(text, _)| *text == target) {
        return Some(key);
    }
    let found = super::did_you_mean(&target, folded.iter().map(|(text, _)| text.as_str()))?;
    folded
        .iter()
        .find(|(text, _)| text == found)
        .map(|(_, key)| *key)
}

fn json_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// `{keys, required}` of a manifest spec object.
fn keys_and_required(schema: &Value) -> (Map<String, Value>, Vec<&str>) {
    let keys = schema["keys"].as_object().cloned().unwrap_or_default();
    let required = schema["required"]
        .as_array()
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    (keys, required)
}

/// Every violation of the operation's manifest `spec` in `spec`, in order:
/// the type, then each key (sorted), then missing required keys.
fn spec_violations(context: &Context<'_>, spec: &Map<String, Value>) -> Vec<String> {
    let schema = &context.operation["spec"];
    let mut check = SpecCheck {
        context,
        violations: Vec::new(),
    };
    if let Some(discriminator) = schema["discriminator"].as_str() {
        let variants = schema["variants"].as_object().cloned().unwrap_or_default();
        let common = schema["common"].as_object().cloned().unwrap_or_default();
        let mut types = variants.keys().map(String::as_str).collect::<Vec<_>>();
        types.sort_unstable();
        let chosen = match spec.get(discriminator) {
            None => {
                check.violations.push(format!(
                    "a job spec needs {discriminator} (schedule or webhook, for example); `{}` shows minimal specs and `{}` prints every type with its config_fields",
                    context.command("job create --help"),
                    context.command("job list")
                ));
                None
            }
            Some(Value::String(kind)) if variants.contains_key(kind) => Some(kind.clone()),
            Some(Value::String(kind)) => {
                let near = super::did_you_mean(kind, types.iter().copied())
                    .map(|near| format!("; did you mean {near}?"))
                    .unwrap_or_default();
                check.violations.push(format!(
                    "{discriminator} {kind_quoted} is not a job type{near} (types: {})",
                    types.join(", "),
                    kind_quoted = crate::error::quote(kind)
                ));
                None
            }
            Some(_) => {
                check.violations.push(format!(
                    "{discriminator} must be a string naming a job type (types: {})",
                    types.join(", ")
                ));
                None
            }
        };
        let mut keys = common;
        let (required, label) = if let Some(kind) = &chosen {
            let (variant_keys, required) = keys_and_required(&variants[kind]);
            keys.extend(variant_keys);
            (required, format!("a {kind} job spec"))
        } else {
            // An unknown type is checked against every variant's keys.
            for variant in variants.values() {
                keys.extend(keys_and_required(variant).0);
            }
            (Vec::new(), "a job spec".to_owned())
        };
        check.object(spec, &keys, &required, "", &label, "");
    } else {
        let (keys, required) = keys_and_required(schema);
        let label = if schema["option"] == "--config-file" {
            "the configuration"
        } else {
            "the spec"
        };
        let hint = format!(
            "; `{}` prints the current values",
            context.command("job show JOB_ID")
        );
        check.object(spec, &keys, &required, "", label, &hint);
        let minimum = schema["minKeys"].as_u64().unwrap_or(0);
        if (spec.len() as u64) < minimum {
            let mut accepted = keys.keys().map(String::as_str).collect::<Vec<_>>();
            accepted.sort_unstable();
            check.violations.push(format!(
                "the spec is empty; give at least one of {}",
                accepted.join(", ")
            ));
        }
    }
    check.violations
}

/// Whether a create spec starts no runs (manifest `spec.unpaidWhen`: a
/// webhook with no program): effect write, and no hold is quoted.
fn unpaid(context: &Context<'_>, spec: &Map<String, Value>) -> bool {
    let rule = &context.operation["spec"]["unpaidWhen"];
    let (Some(kind), Some(absent)) = (rule["type"].as_str(), rule["absent"].as_str()) else {
        return false;
    };
    spec.get("type").and_then(Value::as_str) == Some(kind) && !spec.contains_key(absent)
}

/// Reads a spec or configuration file and checks it against the manifest
/// `spec`; returns the bytes to send and the parsed object.
fn read_spec(
    context: &Context<'_>,
    option: &str,
) -> Result<(Vec<u8>, Map<String, Value>), RunnerError> {
    let value = context.option(option).unwrap_or_default();
    let bytes = super::fs::read_source(&context.system.current_dir, value, SPEC_MAX_BYTES, option)?;
    let text = std::str::from_utf8(&bytes).map_err(|_| {
        invalid(format!(
            "{option} {value_quoted} is not UTF-8 text",
            value_quoted = crate::error::quote(value)
        ))
    })?;
    let parsed = serde_json::from_str::<Value>(text).map_err(|_| {
        invalid(format!(
            "{option} {value_quoted} is not valid JSON",
            value_quoted = crate::error::quote(value)
        ))
    })?;
    let Value::Object(spec) = parsed else {
        return Err(invalid(format!(
            "{option} {value_quoted} must contain a JSON object",
            value_quoted = crate::error::quote(value)
        )));
    };
    let violations = spec_violations(context, &spec);
    match violations.as_slice() {
        [] => Ok((bytes, spec)),
        [one] => Err(invalid(one.clone()).with_detail("violations", json!(violations))),
        many => Err(invalid(format!(
            "{} problems in {option} {value_quoted}: {}",
            many.len(),
            many.iter()
                .enumerate()
                .map(|(index, violation)| format!("({}) {violation}", index + 1))
                .collect::<Vec<_>>()
                .join(" "),
            value_quoted = crate::error::quote(value)
        ))
        .with_detail("violations", json!(violations))),
    }
}

// -------------------------------------------------------------- validators

fn is_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                *byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)
            }
        })
}

fn is_run_id(text: &str) -> bool {
    text.strip_prefix("run_").is_some_and(|rest| {
        (1..=128).contains(&rest.len())
            && rest
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    })
}

fn is_model_id(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'.' || *byte == b'-'
        })
}

fn is_program_ref(text: &str) -> bool {
    text.len() <= 200 && program_ref::parse(text, true).is_ok()
}

fn is_hex64(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// A closed field kind of the result schema.
#[derive(Clone, Copy)]
enum Kind {
    /// Identifier-like text: at most N code points, no control characters.
    Text(usize),
    /// Free service text: control characters become spaces, cut to N.
    Prose(usize),
    Integer,
    EpochMs,
    Bool,
    Uuid,
    RunId,
    ModelId,
    ProgramRef,
    Slug,
    Hex64,
    /// A program revision id: 16 lowercase hex digits.
    RevId,
}

fn scalar(value: &Value, kind: Kind, field: &str) -> Result<Value, RunnerError> {
    let text = || value.as_str().ok_or_else(|| protocol(field));
    let check = |ok: bool| {
        if ok {
            Ok(value.clone())
        } else {
            Err(protocol(field))
        }
    };
    match kind {
        Kind::Text(max) => {
            let text = text()?;
            check(text.is_empty() || valid_text(text, max))
        }
        Kind::Prose(max) => Ok(json!(
            text()?
                .chars()
                .map(|c| if c <= '\u{1f}' || c == '\u{7f}' {
                    ' '
                } else {
                    c
                })
                .take(max)
                .collect::<String>()
        )),
        Kind::Integer => as_integer(value)
            .map(|n| json!(n))
            .ok_or_else(|| protocol(field)),
        Kind::EpochMs => as_integer(value)
            .filter(|n| *n >= 0)
            .map(|n| json!(n))
            .ok_or_else(|| protocol(field)),
        Kind::Bool => check(value.is_boolean()),
        Kind::Uuid => check(is_uuid(text()?)),
        Kind::RunId => check(is_run_id(text()?)),
        Kind::ModelId => check(is_model_id(text()?)),
        Kind::ProgramRef => check(is_program_ref(text()?)),
        Kind::Slug => check(program_ref::valid_slug(text()?)),
        Kind::Hex64 => check(is_hex64(text()?)),
        Kind::RevId => check(program_ref::valid_rev(text()?)),
    }
}

/// Copies `fields` from `source` into `target` under their public
/// `snake_case` names. `nullable` fields accept `null`; a present field of the
/// wrong shape is `SERVICE_PROTOCOL_INVALID`.
fn copy(
    target: &mut Map<String, Value>,
    source: &Map<String, Value>,
    prefix: &str,
    fields: &[(&str, Kind, bool)],
) -> Result<(), RunnerError> {
    for (name, kind, nullable) in fields {
        let Some(value) = source.get(*name) else {
            continue;
        };
        let field = format!("{prefix}.{name}");
        let projected = if value.is_null() {
            if *nullable {
                Value::Null
            } else {
                return Err(protocol(&field));
            }
        } else {
            scalar(value, *kind, &field)?
        };
        target.insert(snake_case(name), projected);
    }
    Ok(())
}

fn object<'a>(value: &'a Value, field: &str) -> Result<&'a Map<String, Value>, RunnerError> {
    value.as_object().ok_or_else(|| protocol(field))
}

fn array<'a>(value: &'a Value, max: usize, field: &str) -> Result<&'a Vec<Value>, RunnerError> {
    value
        .as_array()
        .filter(|items| items.len() <= max)
        .ok_or_else(|| protocol(field))
}

// ------------------------------------------------------------- projections

fn project_run_configuration(value: &Value, field: &str) -> Result<Value, RunnerError> {
    let source = object(value, field)?;
    let mut target = Map::new();
    copy(
        &mut target,
        source,
        field,
        &[
            ("model", Kind::ModelId, false),
            ("environment", Kind::Text(64), false),
            ("reasoning_effort", Kind::Text(32), false),
        ],
    )?;
    if let Some(inputs) = source.get("inputs") {
        let inputs = object(inputs, &format!("{field}.inputs"))?;
        if inputs.len() > 100 || inputs.values().any(|value| !value.is_string()) {
            return Err(protocol(&format!("{field}.inputs")));
        }
        // Money rule: no output property name may match /cost/i. An
        // input so named is a user key, not a money field, and `job configure`
        // replaces every input, so it is kept losslessly as a {name, value}
        // entry of `input_entries` instead of being dropped.
        let (entries, kept): (Vec<_>, Vec<_>) = inputs
            .iter()
            .partition(|(name, _)| name.to_ascii_lowercase().contains("cost"));
        let kept = kept
            .into_iter()
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect::<Map<_, _>>();
        target.insert("inputs".into(), Value::Object(kept));
        if !entries.is_empty() {
            let entries = entries
                .into_iter()
                .map(|(name, value)| json!({"name": name, "value": value}))
                .collect::<Vec<_>>();
            target.insert("input_entries".into(), Value::Array(entries));
        }
    }
    if let Some(files) = source.get("files") {
        let name = format!("{field}.files");
        let mut projected = Vec::new();
        for file in array(files, 64, &name)? {
            let file = object(file, &name)?;
            let mut item = Map::new();
            copy(
                &mut item,
                file,
                &name,
                &[
                    ("name", Kind::Text(256), false),
                    ("size", Kind::EpochMs, false),
                ],
            )?;
            match file.get("content") {
                Some(Value::String(content)) => {
                    item.insert("content".into(), json!(content));
                }
                _ => return Err(protocol(&name)),
            }
            if item.len() != 3 {
                return Err(protocol(&name));
            }
            projected.push(Value::Object(item));
        }
        target.insert("files".into(), Value::Array(projected));
    }
    if let Some(repositories) = source.get("contextRepositories") {
        let name = format!("{field}.contextRepositories");
        let mut projected = Vec::new();
        for repository in array(repositories, 16, &name)? {
            let repository = object(repository, &name)?;
            let mut item = Map::new();
            copy(
                &mut item,
                repository,
                &name,
                &[
                    ("url", Kind::Text(2048), false),
                    ("branch", Kind::Text(256), true),
                ],
            )?;
            if !item.contains_key("url") {
                return Err(protocol(&name));
            }
            projected.push(Value::Object(item));
        }
        target.insert("context_repositories".into(), Value::Array(projected));
    }
    if let Some(output) = source.get("output") {
        if !output.is_null() {
            let name = format!("{field}.output");
            let mut item = Map::new();
            copy(
                &mut item,
                object(output, &name)?,
                &name,
                &[
                    ("type", Kind::Text(32), false),
                    ("repository", Kind::Text(2048), false),
                    ("branch", Kind::Text(256), true),
                ],
            )?;
            if !item.contains_key("type") || !item.contains_key("repository") {
                return Err(protocol(&name));
            }
            target.insert("output".into(), Value::Object(item));
        }
    }
    Ok(Value::Object(target))
}

fn project_contract(value: &Value, field: &str) -> Result<Value, RunnerError> {
    let source = object(value, field)?;
    let mut target = Map::new();
    copy(
        &mut target,
        source,
        field,
        &[
            ("programRef", Kind::ProgramRef, false),
            ("programSlug", Kind::Slug, false),
            ("enabled", Kind::Bool, false),
            ("model", Kind::ModelId, true),
            ("contextRepositoryFullName", Kind::Text(200), true),
            ("contextRepositoryBranch", Kind::Text(256), true),
        ],
    )?;
    if !target.contains_key("program_ref") {
        return Err(protocol(&format!("{field}.program_ref")));
    }
    if let Some(configuration) = source.get("runConfiguration") {
        if !configuration.is_null() {
            target.insert(
                "run_configuration".into(),
                project_run_configuration(configuration, &format!("{field}.run_configuration"))?,
            );
        }
    }
    Ok(Value::Object(target))
}

fn project_contracts(value: &Value, field: &str) -> Result<Value, RunnerError> {
    array(value, 16, field)?
        .iter()
        .map(|contract| project_contract(contract, field))
        .collect::<Result<Vec<_>, _>>()
        .map(Value::Array)
}

/// A job record: its public fields only.
fn project_job(value: &Value) -> Result<Value, RunnerError> {
    let source = object(value, "job")?;
    let mut target = Map::new();
    for required in ["id", "type", "createdAt"] {
        if !source.contains_key(required) || source[required].is_null() {
            return Err(protocol(&format!("job.{}", snake_case(required))));
        }
    }
    copy(
        &mut target,
        source,
        "job",
        &[
            ("id", Kind::Uuid, false),
            ("type", Kind::Text(64), false),
            ("createdAt", Kind::EpochMs, false),
            ("name", Kind::Prose(256), true),
            ("url", Kind::Text(2048), true),
            ("intervalSeconds", Kind::Integer, true),
            ("mode", Kind::Text(64), true),
            ("repositoryId", Kind::Integer, true),
            ("repositoryFullName", Kind::Text(200), true),
            ("repositoryBranch", Kind::Text(256), true),
            ("contextRepositoryFullName", Kind::Text(200), true),
            ("contextRepositoryBranch", Kind::Text(256), true),
            ("programRef", Kind::ProgramRef, true),
            ("programSlug", Kind::Slug, true),
            ("model", Kind::ModelId, true),
            ("lastRunId", Kind::RunId, true),
            ("lastError", Kind::Prose(1024), true),
            ("nextFireAt", Kind::EpochMs, true),
            ("lastEventAt", Kind::EpochMs, true),
        ],
    )?;
    if let Some(contracts) = source.get("contracts") {
        target.insert(
            "contracts".into(),
            project_contracts(contracts, "job.contracts")?,
        );
    }
    add_iso(
        &mut target,
        &["created_at", "next_fire_at", "last_event_at"],
    );
    Ok(Value::Object(target))
}

fn project_status(value: &Value) -> Result<Value, RunnerError> {
    let source = object(value, "status")?;
    let mut target = Map::new();
    copy(
        &mut target,
        source,
        "status",
        &[
            ("configured", Kind::Bool, false),
            ("active", Kind::Bool, false),
            ("deliveryMode", Kind::Text(32), false),
            ("receiverSecretConfigured", Kind::Bool, false),
            ("replySecretConfigured", Kind::Bool, false),
            ("secretRotatedAt", Kind::EpochMs, true),
            ("lastEventAt", Kind::EpochMs, true),
            ("lastRunId", Kind::RunId, true),
            ("lastError", Kind::Prose(1024), true),
            ("intervalSeconds", Kind::Integer, true),
            ("configurationRevision", Kind::Integer, true),
            ("configurationToken", Kind::Hex64, true),
            ("nextFireAt", Kind::EpochMs, true),
            ("lastFiredAt", Kind::EpochMs, true),
        ],
    )?;
    // The optimistic-concurrency token is opaque to the caller: `job
    // configure` sends it back as `revision_token`.
    if let Some(token) = target.remove("configuration_token") {
        target.insert("revision_token".into(), token);
    }
    if let Some(counts) = source.get("counts") {
        let counts = object(counts, "status.counts")?;
        let mut projected = Map::new();
        for (state, folded) in RUN_STATES {
            let mut total = 0_i64;
            for name in folded {
                if let Some(count) = counts.get(*name) {
                    total += as_integer(count)
                        .filter(|n| *n >= 0)
                        .ok_or_else(|| protocol("status.counts"))?;
                }
            }
            projected.insert(state.to_owned(), json!(total));
        }
        target.insert("counts".into(), Value::Object(projected));
    }
    if let Some(contracts) = source.get("contracts") {
        target.insert(
            "contracts".into(),
            project_contracts(contracts, "status.contracts")?,
        );
    }
    add_iso(
        &mut target,
        &[
            "secret_rotated_at",
            "last_event_at",
            "next_fire_at",
            "last_fired_at",
        ],
    );
    Ok(Value::Object(target))
}

/// `{job, status?}` (show, update, configure) plus, for create, the
/// once-only `endpoint` and `signing_secret`, and the absolute `endpoint_url`
/// (on create always; otherwise only when the service's endpoint is the
/// secret-free job-id webhook path).
fn project_detail(
    body: &Map<String, Value>,
    secrets: bool,
    environment: &Environment,
) -> Result<Value, RunnerError> {
    let mut target = Map::new();
    target.insert(
        "job".into(),
        project_job(body.get("trigger").unwrap_or(&Value::Null))?,
    );
    if let Some(status) = body.get("status") {
        if !status.is_null() {
            target.insert("status".into(), project_status(status)?);
        }
    }
    if secrets {
        copy(
            &mut target,
            body,
            "response",
            &[
                ("endpoint", Kind::Text(512), false),
                ("signing_secret", Kind::Text(512), false),
            ],
        )?;
        if let Some(url) = target
            .get("endpoint")
            .and_then(Value::as_str)
            .and_then(|path| absolute_url(environment, path))
        {
            target.insert("endpoint_url".into(), json!(url));
        }
    } else if let (Some(Value::String(path)), Some(id)) = (
        body.get("endpoint"),
        target.get("job").and_then(|job| job["id"].as_str()),
    ) {
        if *path == format!("/webhooks/triggers/{id}") {
            if let Some(url) = absolute_url(environment, path) {
                target.insert("endpoint_url".into(), json!(url));
            }
        }
    }
    Ok(Value::Object(target))
}

fn project_type(value: &Value) -> Result<Value, RunnerError> {
    let source = object(value, "types")?;
    let mut target = Map::new();
    copy(
        &mut target,
        source,
        "types",
        &[
            ("id", Kind::Text(64), false),
            ("label", Kind::Prose(256), false),
            ("description", Kind::Prose(2048), false),
        ],
    )?;
    let fields = array(
        source.get("config_fields").unwrap_or(&Value::Null),
        64,
        "types.config_fields",
    )?
    .iter()
    .map(|item| scalar(item, Kind::Text(64), "types.config_fields"))
    .collect::<Result<Vec<_>, _>>()?;
    target.insert("config_fields".into(), Value::Array(fields));
    for required in ["id", "label", "description"] {
        if !target.contains_key(required) {
            return Err(protocol(&format!("types.{required}")));
        }
    }
    Ok(Value::Object(target))
}

// -------------------------------------------------------------- operations

fn get_json(
    context: &mut Context<'_>,
    index: usize,
    path: &str,
) -> Result<Map<String, Value>, RunnerError> {
    let request = Request::from_manifest(context.operation, index, path);
    context.send(&request)?.json_object()
}

/// The projected jobs and job limit of a job list body (shared with `cli
/// service triage`).
pub(super) fn project_jobs(body: &Map<String, Value>) -> Result<(Vec<Value>, Value), RunnerError> {
    let jobs = array(body.get("triggers").unwrap_or(&Value::Null), 1000, "jobs")?
        .iter()
        .map(project_job)
        .collect::<Result<Vec<_>, _>>()?;
    Ok((jobs, project_job_limit(body)?))
}

/// The account's job limit `{kind, max?}` from the service's `trigger_limit`
/// entitlement. `max_triggers` is a legacy field holding the free ceiling, so
/// it is read only when `trigger_limit` is absent or null (an older service).
/// An unknown kind is `unavailable`, never guessed.
fn project_job_limit(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let limit = match body.get("trigger_limit") {
        None | Some(Value::Null) => {
            return Ok(match body.get("max_triggers").and_then(as_integer) {
                Some(max) if max >= 0 => json!({"kind": "limited", "max": max}),
                _ => json!({"kind": "unavailable"}),
            });
        }
        Some(value) => object(value, "job_limit")?,
    };
    let kind = limit
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(|| protocol("job_limit.kind"))?;
    Ok(match kind {
        "limited" => {
            let max = limit
                .get("max")
                .and_then(as_integer)
                .filter(|max| *max >= 0)
                .ok_or_else(|| protocol("job_limit.max"))?;
            json!({"kind": "limited", "max": max})
        }
        "unlimited" => json!({"kind": "unlimited"}),
        _ => json!({"kind": "unavailable"}),
    })
}

/// `max_jobs` / triage `jobs.max`: the job limit's max when limited, else
/// null.
pub(super) fn limit_max(job_limit: &Value) -> Value {
    if job_limit["kind"] == "limited" {
        job_limit["max"].clone()
    } else {
        Value::Null
    }
}

/// The human `Jobs:` line for `total` jobs under `job_limit`.
pub(super) fn jobs_line(total: usize, job_limit: &Value) -> String {
    match (job_limit["kind"].as_str(), job_limit["max"].as_i64()) {
        (Some("limited"), Some(max)) => format!("Jobs: {total} of {max} allowed\n"),
        (Some("unlimited"), _) => format!("Jobs: {total} (unlimited)\n"),
        _ => format!("Jobs: {total} (limit unavailable; try again)\n"),
    }
}

fn list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let body = get_json(context, 0, "/triggers")?;
    let (jobs, job_limit) = project_jobs(&body)?;
    let types = array(body.get("types").unwrap_or(&Value::Null), 64, "types")?
        .iter()
        .map(project_type)
        .collect::<Result<Vec<_>, _>>()?;
    let result = json!({
        "jobs": jobs,
        "max_jobs": limit_max(&job_limit),
        "job_limit": job_limit,
        "types": types,
    });
    context.human = Some(human_list(&result));
    Ok(result)
}

fn show(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let body = get_json(context, 0, &format!("/triggers/{}", encode_segment(&id)))?;
    let result = project_detail(&body, false, &context.environment)?;
    context.human = Some(human_detail(&result));
    Ok(result)
}

fn create(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let (mut body, mut spec) = read_spec(context, "--spec-file")?;
    // A program reference is pinned before the plan: a bare SLUG, `@N` or a
    // latest OWNER/SLUG resolves to OWNER/SLUG@REV (requests 2 and 3), and
    // the job stores the pinned reference.
    if let Some(given) = spec
        .get("program_ref")
        .and_then(Value::as_str)
        .map(str::to_owned)
    {
        let mut reference = program_ref::parse_own_allowed_with(&given, true)?;
        if reference.pinned().is_none() || reference.owner.is_empty() {
            let pinned =
                program_ref::resolve_to_run(context, &mut reference, 2, Some(3), None, false)?;
            if pinned != given {
                spec.insert("program_ref".into(), json!(pinned));
                body = canonical(&Value::Object(spec.clone())).into_bytes();
            }
        }
    }
    let mut planned = context.planned(1, "/triggers", &[], Some(&body));
    if unpaid(context, &spec) {
        // A webhook with no program starts no runs: nothing is held.
        planned["effect"] = json!("write");
    } else if context.invocation.preview || !context.invocation.yes {
        planned["quote"] = quote(context, &spec)?;
    }
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let mut request = Request::from_manifest(context.operation, 1, "/triggers");
    request.body = Some(body);
    let response = context.send(&request)?.json_object()?;
    let result = project_detail(&response, true, &context.environment)?;
    if result.get("signing_secret").is_some() && context.mode == crate::OutputMode::Human {
        let _ = context.err.write_all(SECRET_WARNING.as_bytes());
    }
    context.human = Some(human_detail(&result));
    Ok(result)
}

/// The `GET /run/quote` hold for the confirmation plan, sent with the key.
/// It names the pinned `program_ref` the job stores, so the service prices
/// that program with its own run settings and declared tools, and sends the
/// hold options the spec gives (`model`, `reasoning_effort`, `environment`,
/// and `repositories=1` for a `repository_url` or `context_repository_url`)
/// as overrides, never defaults. The price policy reference stays internal.
fn quote(context: &mut Context<'_>, spec: &Map<String, Value>) -> Result<Value, RunnerError> {
    let text = |key: &str| {
        spec.get(key)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
    };
    let repositories_bound =
        text("repository_url").is_some() || text("context_repository_url").is_some();
    let request = quote_request(
        context,
        0,
        &HoldInputs {
            program_ref: text("program_ref"),
            model: text("model"),
            reasoning_effort: text("reasoning_effort"),
            environment: text("environment"),
            repositories_bound,
            job_type: text("type"),
        },
    );
    send_quote(context, &request)
}

/// What a plan quote prices: the program and the run settings it will use.
struct HoldInputs<'a> {
    program_ref: Option<&'a str>,
    model: Option<&'a str>,
    reasoning_effort: Option<&'a str>,
    environment: Option<&'a str>,
    repositories_bound: bool,
    job_type: Option<&'a str>,
}

/// The `GET /run/quote` request (manifest request `index`) for `inputs`.
fn quote_request(context: &Context<'_>, index: usize, inputs: &HoldInputs<'_>) -> Request {
    let mut request = Request::from_manifest(context.operation, index, "/run/quote");
    if let Some(program) = inputs.program_ref {
        request = request.query("program_ref", program.to_owned());
    }
    let mut request = super::runs::hold_query(
        request,
        inputs.model,
        inputs.reasoning_effort,
        inputs.environment,
        inputs.repositories_bound,
    );
    // The job's type lets the service price the model a job of that type
    // runs on when the plan names none.
    if let Some(job_type) = inputs.job_type {
        request = request.query("job_type", job_type.to_owned());
    }
    request
}

/// Sends a plan quote and projects its `{hold}`.
fn send_quote(context: &mut Context<'_>, request: &Request) -> Result<Value, RunnerError> {
    let body = context.send(request)?.json_object()?;
    let hold = object(body.get("hold").unwrap_or(&Value::Null), "quote.hold")?;
    let hold_usd = hold
        .get("hold_usd")
        .and_then(Value::as_str)
        .filter(|text| {
            let digits = |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit());
            text.strip_prefix('-')
                .unwrap_or(text)
                .split_once('.')
                .is_some_and(|(whole, cents)| digits(whole) && cents.len() == 2 && digits(cents))
        })
        .ok_or_else(|| protocol("quote.hold.hold_usd"))?;
    let hold_cents =
        super::render::usd_cents(hold_usd).ok_or_else(|| protocol("quote.hold.hold_usd"))?;
    let ttl = scalar(
        hold.get("ttl_seconds").unwrap_or(&Value::Null),
        Kind::EpochMs,
        "quote.hold.ttl_seconds",
    )?;
    Ok(json!({
        "hold": {"hold_usd": hold_usd, "hold_cents": hold_cents, "ttl_seconds": ttl},
    }))
}

fn update(context: &mut Context<'_>, configure: bool) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let body = if configure {
        configuration_body(context, &id)?
    } else {
        read_spec(context, "--spec-file")?.0
    };
    let path = if configure {
        format!("/triggers/{}/configuration", encode_segment(&id))
    } else {
        format!("/triggers/{}", encode_segment(&id))
    };
    if let Gate::Preview(result) = context.gate(context.planned(0, &path, &[], Some(&body)))? {
        return Ok(result);
    }
    let mut request = Request::from_manifest(context.operation, 0, path);
    request.body = Some(body);
    let response = match context.send(&request) {
        // PUT /triggers/{id} changes webhook jobs only.
        Err(error) if !configure && service_status(&error) == Some(405) => {
            return Err(error.with_detail(
                "reason",
                format!(
                    "`cli job update` changes webhook jobs only; change a schedule job with `cli job configure {id} --interval-seconds N --yes` or `cli job configure {id} --config-file FILE --yes`, and see details.serviceMessage for other job types"
                ),
            ));
        }
        other => other?.json_object()?,
    };
    let result = project_detail(&response, false, &context.environment)?;
    context.human = Some(human_detail(&result));
    Ok(result)
}

/// The HTTP status a classified service error carries.
fn service_status(error: &RunnerError) -> Option<u64> {
    error.details.as_ref()?.get("serviceStatus")?.as_u64()
}

/// The `job configure` body: the `--config-file` configuration, or, with
/// `--interval-seconds N`, the job's current revision, token and bindings
/// read from the job (manifest request 1) with the new interval;
/// `input_entries` is merged back into `inputs`. Either way the public
/// names are translated to the service's ([`service_configuration`]).
fn configuration_body(context: &mut Context<'_>, id: &str) -> Result<Vec<u8>, RunnerError> {
    let interval = match (
        context.option("--config-file"),
        context.option("--interval-seconds"),
    ) {
        (Some(_), Some(_)) => {
            return Err(invalid(
                "give exactly one of --config-file and --interval-seconds",
            ));
        }
        (None, None) => {
            return Err(invalid(format!(
                "give --interval-seconds N to change only the cadence, or --config-file FILE with the full configuration; see `cli job configure --help` ({})",
                interval_reason()
            )));
        }
        (Some(_), None) => {
            let (_, configuration) = read_spec(context, "--config-file")?;
            return Ok(
                canonical(&service_configuration(Value::Object(configuration))).into_bytes(),
            );
        }
        (None, Some(value)) => Some(value)
            .filter(|value| {
                (1..=7).contains(&value.len()) && value.bytes().all(|b| b.is_ascii_digit())
            })
            .and_then(|value| value.parse::<i64>().ok())
            .filter(|n| (INTERVAL_MIN..=INTERVAL_MAX).contains(n))
            .ok_or_else(|| invalid(format!("--interval-seconds: {}", interval_reason())))?,
    };
    let body = get_json(context, 1, &format!("/triggers/{}", encode_segment(id)))?;
    let detail = project_detail(&body, false, &context.environment)?;
    let kind = detail["job"]["type"].as_str().unwrap_or_default();
    if kind != "schedule" {
        return Err(invalid(format!(
            "--interval-seconds applies to schedule jobs only; job {id} is a {} job (change webhook settings with `cli job update`)",
            human_safe_scalar(kind)
        )));
    }
    let status = &detail["status"];
    let revision = status["configuration_revision"]
        .as_i64()
        .ok_or_else(|| protocol("status.configuration_revision"))?;
    let token = status["revision_token"]
        .as_str()
        .ok_or_else(|| protocol("status.revision_token"))?;
    let contracts = status["contracts"]
        .as_array()
        .filter(|contracts| !contracts.is_empty())
        .ok_or_else(|| protocol("status.contracts"))?;
    let mut bindings = Vec::new();
    for contract in contracts {
        let mut configuration = if let Value::Object(configuration) = &contract["run_configuration"]
        {
            configuration.clone()
        } else {
            let mut fallback = Map::new();
            if let Some(model) = contract["model"].as_str() {
                fallback.insert("model".into(), json!(model));
            }
            fallback
        };
        if let Some(Value::Array(entries)) = configuration.remove("input_entries") {
            let inputs = configuration
                .entry("inputs")
                .or_insert_with(|| Value::Object(Map::new()));
            if let Value::Object(inputs) = inputs {
                for entry in entries {
                    if let (Some(name), Some(value)) = (entry["name"].as_str(), entry.get("value"))
                    {
                        inputs.insert(name.to_owned(), value.clone());
                    }
                }
            }
        }
        bindings.push(json!({
            "program_ref": contract["program_ref"],
            "run_configuration": Value::Object(configuration),
        }));
    }
    Ok(canonical(&service_configuration(json!({
        "interval_seconds": interval,
        "configuration_revision": revision,
        "revision_token": token,
        "bindings": bindings,
    })))
    .into_bytes())
}

/// A public `job configure` configuration in the service's names: the
/// opaque `revision_token` is sent as the service's configuration token and
/// each binding's `run_configuration.context_repositories` under the
/// service's camelCase name (identical in both ports).
fn service_configuration(mut configuration: Value) -> Value {
    if let Some(object) = configuration.as_object_mut() {
        if let Some(token) = object.remove("revision_token") {
            object.insert("configuration_token".into(), token);
        }
        if let Some(Value::Array(bindings)) = object.get_mut("bindings") {
            for binding in bindings {
                if let Some(run) = binding
                    .get_mut("run_configuration")
                    .and_then(Value::as_object_mut)
                {
                    if let Some(repositories) = run.remove("context_repositories") {
                        run.insert("contextRepositories".into(), repositories);
                    }
                }
            }
        }
    }
    configuration
}

/// The `<JOB_ID>` of `job delete`: a lowercase UUID as
/// `cli job list` prints it, checked before any request. A malformed id could
/// otherwise reach a 404 (or a normalized path such as `..`) that the
/// idempotent delete would report as `already_absent`.
fn delete_id(context: &Context<'_>) -> Result<String, RunnerError> {
    let id = job_id(context)?;
    if is_uuid(&id) {
        return Ok(id);
    }
    let lower = id.to_lowercase();
    let hint = if is_uuid(&lower) {
        format!(
            "; pass {lower_quoted}",
            lower_quoted = crate::error::quote(&lower)
        )
    } else {
        String::new()
    };
    let words = ["job", "list"];
    let mut error = invalid(format!(
        "JOB_ID {id_quoted} is not a job id: job ids are lowercase UUIDs (8-4-4-4-12 hex digits) as `{}` prints them; nothing was sent{hint}",
        context.command("job list"),
        id_quoted = crate::error::quote(&id)
    ));
    error.action = format!(
        "List your jobs with `{}` and pass one of their ids.",
        context.command("job list")
    );
    Err(error.with_detail("suggestedArgv", json!(context.follow_up_argv(&words))))
}

fn delete(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = delete_id(context)?;
    let path = format!("/triggers/{}", encode_segment(&id));
    if let Gate::Preview(result) = context.gate(context.planned(0, &path, &[], None))? {
        return Ok(result);
    }
    let request = Request::from_manifest(context.operation, 0, path);
    let response = match context.send(&request) {
        Ok(response) => response.json_object()?,
        // A confirmed delete is idempotent: nothing to delete
        // is the goal state.
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => {
            context.human = Some(format!(
                "Job {} was already absent; nothing was deleted.\n",
                human_safe_scalar(&id)
            ));
            return Ok(json!({"id": id, "deleted": true, "already_absent": true}));
        }
        Err(error) => return Err(error),
    };
    if response.get("deleted") != Some(&Value::Bool(true)) {
        return Err(protocol("deleted"));
    }
    context.human = Some(format!("Deleted job {}.\n", human_safe_scalar(&id)));
    Ok(json!({"id": id, "deleted": true}))
}

fn deliveries(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let body = match get_json(
        context,
        0,
        &format!("/triggers/{}/deliveries", encode_segment(&id)),
    ) {
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => {
            return Err(explain_missing_deliveries(context, &id, error));
        }
        other => other?,
    };
    let mut projected = Vec::new();
    for delivery in array(
        body.get("deliveries").unwrap_or(&Value::Null),
        100,
        "deliveries",
    )? {
        let source = object(delivery, "deliveries")?;
        let mut target = Map::new();
        copy(
            &mut target,
            source,
            "deliveries",
            &[
                ("id", Kind::Integer, false),
                ("receivedAt", Kind::EpochMs, false),
                ("outcome", Kind::Text(32), false),
                ("testOnly", Kind::Bool, false),
                ("deliveryId", Kind::Text(256), true),
                ("reason", Kind::Prose(1024), true),
            ],
        )?;
        for required in ["id", "received_at", "outcome", "test_only"] {
            if !target.contains_key(required) {
                return Err(protocol(&format!("deliveries.{required}")));
            }
        }
        add_iso(&mut target, &["received_at"]);
        // The runs the delivery started.
        if let Some(runs) = source.get("jobs") {
            let mut items = Vec::new();
            for run in array(runs, 32, "deliveries.runs")? {
                let run = object(run, "deliveries.runs")?;
                let mut item = Map::new();
                copy(
                    &mut item,
                    run,
                    "deliveries.runs",
                    &[
                        ("programRef", Kind::ProgramRef, false),
                        ("status", Kind::Text(32), false),
                        ("runId", Kind::RunId, true),
                    ],
                )?;
                if item.len() != 3 {
                    return Err(protocol("deliveries.runs"));
                }
                items.push(Value::Object(item));
            }
            target.insert("runs".into(), Value::Array(items));
        }
        projected.push(Value::Object(target));
    }
    let result = json!({"deliveries": projected});
    context.human = Some(human_deliveries(&result));
    Ok(result)
}

/// The service answers 404 for the deliveries of any job that is not a
/// webhook. Reading the job (manifest request 1) tells a wrong job type from
/// an unknown id; any other outcome keeps the original 404.
fn explain_missing_deliveries(
    context: &mut Context<'_>,
    id: &str,
    original: RunnerError,
) -> RunnerError {
    let Ok(body) = get_json(context, 1, &format!("/triggers/{}", encode_segment(id))) else {
        return original;
    };
    let kind = body
        .get("trigger")
        .and_then(|trigger| trigger.get("type"))
        .and_then(Value::as_str)
        .filter(|kind| {
            (1..=64).contains(&kind.len())
                && kind
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
        });
    match kind {
        Some(kind) if kind != "webhook" => RunnerError::catalog(ErrorCode::ServiceRequestRejected)
            .with_detail("serviceStatus", 404)
            .with_detail(
                "reason",
                format!(
                    "job {id} is a {kind} job; deliveries exist only for webhook jobs. See its runs with `cli job show {id}` and `cli run list`"
                ),
            ),
        _ => original,
    }
}

fn rotate_secret(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let path = format!("/triggers/{}/rotate-secret", encode_segment(&id));
    if let Gate::Preview(result) = context.gate(context.planned(0, &path, &[], None))? {
        return Ok(result);
    }
    let request = Request::from_manifest(context.operation, 0, path);
    let response = context.send(&request)?.json_object()?;
    let mut result = Map::new();
    result.insert("id".into(), json!(id));
    copy(
        &mut result,
        &response,
        "response",
        &[
            ("endpoint", Kind::Text(512), false),
            ("signing_secret", Kind::Text(512), false),
        ],
    )?;
    for required in ["endpoint", "signing_secret"] {
        if !result.contains_key(required) {
            return Err(protocol(required));
        }
    }
    if let Some(url) = result
        .get("endpoint")
        .and_then(Value::as_str)
        .and_then(|path| absolute_url(&context.environment, path))
    {
        result.insert("endpoint_url".into(), json!(url));
    }
    if context.mode == crate::OutputMode::Human {
        let _ = context.err.write_all(SECRET_WARNING.as_bytes());
    }
    let result = Value::Object(result);
    context.human = Some(human_rotated(&result));
    Ok(result)
}

fn contract_list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let body = get_json(
        context,
        0,
        &format!("/triggers/{}/contracts", encode_segment(&id)),
    )?;
    let contracts = listed_contracts(&body)?
        .iter()
        .enumerate()
        .map(|(index, contract)| project_listed_contract(contract, index))
        .collect::<Result<Vec<_>, _>>()?;
    let mut result = Map::new();
    result.insert("contracts".into(), Value::Array(contracts));
    if let Some(max) = body.get("max_contracts") {
        result.insert(
            "max_contracts".into(),
            scalar(max, Kind::EpochMs, "max_contracts")?,
        );
    }
    let result = Value::Object(result);
    context.human = Some(human_contracts(&result));
    Ok(result)
}

/// The contract objects of a `GET /triggers/{id}/contracts` body.
fn listed_contracts(body: &Map<String, Value>) -> Result<&Vec<Value>, RunnerError> {
    let contracts = array(
        body.get("contracts").unwrap_or(&Value::Null),
        16,
        "contracts",
    )?;
    if contracts.iter().any(|contract| !contract.is_object()) {
        return Err(protocol("contracts"));
    }
    Ok(contracts)
}

/// A stored input value as text: a string as it is, another JSON scalar
/// as its JSON text; an object or array is not an input.
fn input_text(value: &Value, field: &str) -> Result<Value, RunnerError> {
    match value {
        Value::String(_) => Ok(value.clone()),
        Value::Object(_) | Value::Array(_) => Err(protocol(field)),
        other => Ok(Value::String(other.to_string())),
    }
}

/// One contract of `GET /triggers/{id}/contracts`: the public contract
/// fields, and its saved execution settings as `run_configuration` (inputs
/// under the money rule, stored file metadata only). Program text and file
/// content are never projected.
fn project_listed_contract(contract: &Value, index: usize) -> Result<Value, RunnerError> {
    let field = "contracts";
    let source = object(contract, field)?;
    // The contract route names differ from the job record's; both project
    // to the same public contract fields.
    let mut renamed = Map::new();
    for (from, to) in [
        ("program_ref", "programRef"),
        ("slug", "programSlug"),
        ("enabled", "enabled"),
        ("model", "model"),
    ] {
        if let Some(value) = source.get(from) {
            renamed.insert(to.to_owned(), value.clone());
        }
    }
    let Value::Object(mut target) = project_contract(&Value::Object(renamed), field)? else {
        return Err(protocol(field));
    };
    copy(
        &mut target,
        source,
        field,
        &[
            ("effective_model", Kind::ModelId, true),
            ("rev_id", Kind::RevId, false),
            ("bound_at", Kind::EpochMs, false),
            ("is_platform_default", Kind::Bool, false),
        ],
    )?;
    add_iso(&mut target, &["bound_at"]);
    let present = |name: &str| source.get(name).filter(|value| !value.is_null());
    let mut configuration = Map::new();
    if let Some(effort) = present("reasoning_effort") {
        configuration.insert("reasoning_effort".into(), effort.clone());
    }
    if let Some(inputs) = present("inputs") {
        let name = format!("{field}.inputs");
        let inputs = object(inputs, &name)?;
        if !inputs.is_empty() {
            let inputs = inputs
                .iter()
                .map(|(key, value)| {
                    let field = format!("contracts[{index}].inputs.{key}");
                    Ok((key.clone(), input_text(value, &field)?))
                })
                .collect::<Result<Map<_, _>, RunnerError>>()?;
            configuration.insert("inputs".into(), Value::Object(inputs));
        }
    }
    if let Some(repositories) = present("repositories") {
        if !array(repositories, 16, &format!("{field}.repositories"))?.is_empty() {
            configuration.insert("contextRepositories".into(), repositories.clone());
        }
    }
    for name in ["environment", "output"] {
        if let Some(value) = present(name) {
            configuration.insert(name.into(), value.clone());
        }
    }
    let mut projected = if configuration.is_empty() {
        Map::new()
    } else {
        match project_run_configuration(
            &Value::Object(configuration),
            &format!("{field}.run_configuration"),
        )? {
            Value::Object(projected) => projected,
            _ => return Err(protocol(field)),
        }
    };
    if let Some(files) = present("files") {
        let name = format!("{field}.files");
        let mut stored = Vec::new();
        for file in array(files, 64, &name)? {
            let mut item = Map::new();
            copy(
                &mut item,
                object(file, &name)?,
                &name,
                &[
                    ("name", Kind::Text(256), false),
                    ("size", Kind::EpochMs, false),
                    ("sha256", Kind::Hex64, false),
                ],
            )?;
            if !item.contains_key("name") || !item.contains_key("size") {
                return Err(protocol(&name));
            }
            stored.push(Value::Object(item));
        }
        if !stored.is_empty() {
            projected.insert("stored_files".into(), Value::Array(stored));
        }
    }
    if !projected.is_empty() {
        target.insert("run_configuration".into(), Value::Object(projected));
    }
    Ok(Value::Object(target))
}

/// Why a contract reference is not pinned, with the command that prints the
/// `rev_id` for the program the caller named: `program show`
/// resolves `@N` and prints the pinned reference as `ref`.
fn unpinned_reason(context: &Context<'_>, value: &str) -> String {
    let (name, rev) = value.split_once('@').unwrap_or((value, ""));
    let named = name.split_once('/').is_some_and(|(owner, slug)| {
        program_ref::valid_owner(owner) && program_ref::valid_slug(slug)
    });
    if named && program_ref::valid_rev_number(rev) {
        return format!(
            "program reference {value_quoted} names revision {rev} by number; a contract pins the 16-hex-digit rev_id, which `{}` prints as `ref`",
            context.command(&format!("program show {value} --json")),
            value_quoted = crate::error::quote(value)
        );
    }
    let show = context.command(&format!(
        "program show {} --json",
        if named { name } else { "OWNER/SLUG" }
    ));
    format!(
        "program reference {value_quoted} must be pinned as OWNER/SLUG@REV (the 16-hex-digit rev_id); `{show}` prints the latest as `ref`",
        value_quoted = crate::error::quote(value)
    )
}

/// The pinned `OWNER/SLUG@REV` argument of `job contract attach|detach`.
fn contract_reference(context: &Context<'_>) -> Result<String, RunnerError> {
    let value = context.argument("OWNER/SLUG@REV").unwrap_or_default();
    Ok(program_ref::parse(value, true)
        .map_err(|error| error.with_detail("reason", unpinned_reason(context, value)))?
        .pinned()
        .unwrap_or_default())
}

fn contract_change(context: &mut Context<'_>, attach: bool) -> Result<Value, RunnerError> {
    if attach {
        return contract_attach(context);
    }
    let id = job_id(context)?;
    let reference = contract_reference(context)?;
    let path = format!(
        "/triggers/{}/contracts/{}",
        encode_segment(&id),
        encode_segment(&reference)
    );
    if let Gate::Preview(result) = context.gate(context.planned(0, &path, &[], None))? {
        return Ok(result);
    }
    let request = Request::from_manifest(context.operation, 0, path);
    let response = context.send(&request)?.json_object()?;
    if response.get("unbound").and_then(Value::as_str) != Some(reference.as_str()) {
        return Err(protocol("unbound"));
    }
    context.human = Some(format!(
        "Detached {} from job {}.\nList the job's contracts with `cli job contract list {}`.\n",
        human_safe_scalar(&reference),
        human_safe_scalar(&id),
        human_safe_scalar(&id),
    ));
    Ok(json!({"contracts": [{"program_ref": reference}]}))
}

/// The value options of `job contract attach` that only a webhook binding
/// takes (any of them, or a flag below, reads the job first).
const BINDING_OPTIONS: [&str; 10] = [
    "--model",
    "--reasoning-effort",
    "--repo",
    "--commit-output",
    "--input",
    "--inputs-file",
    "--clear-input",
    "--environment",
    "--replace",
    "--file",
];
const BINDING_FLAGS: [&str; 3] = ["--clear-repo", "--clear-commit-output", "--clear-files"];

/// `--file` limits: files per binding, bytes per file and bytes in all.
const MAX_BINDING_FILES: usize = 20;
const MAX_BINDING_FILE_BYTES: u64 = 5 << 20;
const MAX_BINDING_FILES_BYTES: u64 = 10 << 20;

/// The binding settings given to `job contract attach`, checked before any
/// request.
struct BindingOptions {
    model: Option<String>,
    effort: Option<String>,
    repo: Option<Repository>,
    commit: Option<Repository>,
    clear_repo: bool,
    clear_commit: bool,
    /// `--inputs-file` and `--input`, when either is given.
    inputs: Option<BTreeMap<String, String>>,
    clear_inputs: Vec<String>,
    environment: Option<String>,
    replace: Option<String>,
    /// `--file`: each NAME with its content in base64, when given.
    files: Option<BTreeMap<String, String>>,
    clear_files: bool,
}

/// `--file [NAME=]PATH` values: UTF-8 files by NAME (default the path's
/// basename), checked against the binding limits.
fn parse_binding_files(context: &Context<'_>) -> Result<BTreeMap<String, String>, RunnerError> {
    let values = context.invocation.option_values("--file");
    if values.len() > MAX_BINDING_FILES {
        return Err(invalid(format!(
            "--file was given {} times; a binding stores at most {MAX_BINDING_FILES} files",
            values.len()
        )));
    }
    let cwd = context.system.current_dir.clone();
    let mut files = BTreeMap::new();
    let mut total = 0_u64;
    for value in values {
        let (name, path) = if let Some((name, path)) = value.split_once('=') {
            (name.to_owned(), path)
        } else {
            let base = std::path::Path::new(value)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or_default();
            (base.to_owned(), value.as_str())
        };
        if !valid_text(&name, 256) || name.contains(['/', '\\']) {
            return Err(invalid(format!(
                "--file {value_quoted}: the file name must be 1 to 256 characters without /, \\ or control characters; give it as NAME=PATH",
                value_quoted = crate::error::quote(value)
            )));
        }
        let bytes = super::fs::read_source(&cwd, path, MAX_BINDING_FILE_BYTES, "--file")?;
        if std::str::from_utf8(&bytes).is_err() {
            return Err(invalid(format!(
                "--file {path_quoted} is not UTF-8 text",
                path_quoted = crate::error::quote(path)
            )));
        }
        total += bytes.len() as u64;
        if total > MAX_BINDING_FILES_BYTES {
            return Err(invalid(format!(
                "the --file files hold more than {MAX_BINDING_FILES_BYTES} bytes together; a binding stores at most that much"
            )));
        }
        if files
            .insert(name.clone(), crate::registry::base64(&bytes))
            .is_some()
        {
            return Err(invalid(format!(
                "--file name {name_quoted} was given more than once; name each file uniquely with NAME=PATH",
                name_quoted = crate::error::quote(&name)
            )));
        }
    }
    Ok(files)
}

impl BindingOptions {
    fn any(context: &Context<'_>) -> bool {
        BINDING_OPTIONS
            .iter()
            .any(|option| !context.invocation.option_values(option).is_empty())
            || BINDING_FLAGS.iter().any(|flag| context.flag(flag))
    }

    /// Checked in a fixed order shared by both ports: conflicts, then
    /// `--clear-input` names, inputs, values and finally `--file` reads.
    fn parse(context: &Context<'_>) -> Result<Self, RunnerError> {
        let clear_repo = context.flag("--clear-repo");
        let clear_commit = context.flag("--clear-commit-output");
        let clear_files = context.flag("--clear-files");
        let given = |option: &str| context.option(option).is_some();
        if given("--repo") && clear_repo {
            return Err(invalid(
                "--repo and --clear-repo cannot be combined: --repo replaces the saved repository, --clear-repo removes it",
            ));
        }
        for (flag, set) in [
            ("--clear-repo", clear_repo),
            ("--clear-commit-output", clear_commit),
        ] {
            if given("--commit-output") && set {
                return Err(invalid(format!(
                    "--commit-output and {flag} cannot be combined: --commit-output sets the commit output, {flag} removes it"
                )));
            }
        }
        if given("--file") && clear_files {
            return Err(invalid(
                "--file and --clear-files cannot be combined: --file replaces the stored files, --clear-files removes them",
            ));
        }
        let clear_inputs = context.invocation.option_values("--clear-input").to_vec();
        for key in &clear_inputs {
            if !runs::valid_input_key(key) {
                return Err(invalid(format!(
                    "--clear-input {key_quoted} must be an input name of 1 to 128 characters without control characters",
                    key_quoted = crate::error::quote(key)
                )));
            }
        }
        for key in &clear_inputs {
            let set = context
                .invocation
                .option_values("--input")
                .iter()
                .any(|raw| raw.split_once('=').is_some_and(|(name, _)| name == key));
            if set {
                return Err(invalid(format!(
                    "--input {key_quoted} and --clear-input {key_quoted} cannot be combined: --input sets the input, --clear-input removes it",
                    key_quoted = crate::error::quote(key)
                )));
            }
        }
        let inputs = (given("--inputs-file")
            || !context.invocation.option_values("--input").is_empty())
        .then(|| runs::parse_inputs(context))
        .transpose()?;
        let model = context.option("--model").map(str::to_owned);
        if let Some(model) = model.as_deref().filter(|model| !runs::valid_model(model)) {
            return Err(invalid(format!(
                "--model {model_quoted} is not a model id; list them with `{}`",
                context.command("model list"),
                model_quoted = crate::error::quote(model)
            )));
        }
        let repo = context
            .option("--repo")
            .map(|value| runs::parse_repository("--repo", value))
            .transpose()?;
        let commit = context
            .option("--commit-output")
            .map(|value| runs::parse_repository("--commit-output", value))
            .transpose()?;
        let environment = context.option("--environment").map(str::to_owned);
        if let Some(environment) = environment
            .as_deref()
            .filter(|value| !runs::valid_token(value))
        {
            return Err(invalid(format!(
                "--environment {value_quoted} is not an environment id (lowercase letters, digits, _ and -)",
                value_quoted = crate::error::quote(environment)
            )));
        }
        let replace = match context.option("--replace") {
            Some(value) => Some(
                program_ref::parse(value, true)
                    .map_err(|error| {
                        error.with_detail(
                            "reason",
                            format!("--replace: {}", unpinned_reason(context, value)),
                        )
                    })?
                    .pinned()
                    .unwrap_or_default(),
            ),
            None => None,
        };
        // Known locally when --repo is given; otherwise checked against the
        // saved repository once the contracts are read.
        if let (Some(commit), Some(repo)) = (&commit, &repo) {
            if !same_url(&commit.url(), &repo.url()) {
                return Err(runs::commit_output_mismatch(commit));
            }
        }
        let files = given("--file")
            .then(|| parse_binding_files(context))
            .transpose()?;
        Ok(Self {
            model,
            effort: context.option("--reasoning-effort").map(str::to_owned),
            repo,
            commit,
            clear_repo,
            clear_commit,
            inputs,
            clear_inputs,
            environment,
            replace,
            files,
            clear_files,
        })
    }

    /// `files`: the given set (it replaces the stored one), or a JSON null
    /// for `--clear-files` when `nulls` are allowed.
    fn put_files(&self, body: &mut Map<String, Value>, nulls: bool) {
        if let Some(files) = &self.files {
            body.insert("files".into(), json!(files));
        } else if self.clear_files && nulls {
            body.insert("files".into(), Value::Null);
        }
    }

    /// The inputs after these options: `saved`, then the given keys, minus
    /// the cleared keys; `None` when the input options change nothing (none
    /// given, or only an empty `--inputs-file`).
    fn merged_inputs(&self, saved: Option<&Map<String, Value>>) -> Option<Map<String, Value>> {
        if self.inputs.as_ref().is_none_or(BTreeMap::is_empty) && self.clear_inputs.is_empty() {
            return None;
        }
        let mut inputs = saved.cloned().unwrap_or_default();
        for (key, value) in self.inputs.iter().flatten() {
            inputs.insert(key.clone(), json!(value));
        }
        for key in &self.clear_inputs {
            inputs.remove(key);
        }
        Some(inputs)
    }
}

/// Repository URLs name the same repository (GitHub names ignore case).
fn same_url(left: &str, right: &str) -> bool {
    left.eq_ignore_ascii_case(right)
}

/// `{type: "commit", repository, branch?}`: the repository is the effective
/// repository's URL as the binding stores it, the branch `--commit-output`'s.
fn commit_output(url: &str, commit: &Repository) -> Value {
    let mut output = Map::new();
    output.insert("type".into(), json!("commit"));
    output.insert("repository".into(), json!(url));
    if let Some(branch) = &commit.branch {
        output.insert("branch".into(), json!(branch));
    }
    Value::Object(output)
}

/// This invocation's argv without the webhook-only binding options.
fn argv_without_binding_options(argv: &[String]) -> Vec<String> {
    argv_without(argv, &BINDING_OPTIONS, &BINDING_FLAGS)
}

/// `argv` without the value `options` (and their values) and `flags`.
fn argv_without(argv: &[String], options: &[&str], flags: &[&str]) -> Vec<String> {
    let mut kept = Vec::new();
    let mut tokens = argv.iter();
    while let Some(token) = tokens.next() {
        if token == "--" {
            kept.push(token.clone());
            kept.extend(tokens.by_ref().cloned());
            break;
        }
        if flags.contains(&token.as_str()) {
            continue;
        }
        if options.contains(&token.as_str()) {
            tokens.next();
            continue;
        }
        let inline = token
            .split_once('=')
            .is_some_and(|(name, _)| options.contains(&name));
        if !inline {
            kept.push(token.clone());
        }
    }
    kept
}

/// The saved settings of one listed binding, as the service returned them.
#[derive(Default)]
struct SavedBinding {
    model: Option<Value>,
    effort: Option<Value>,
    repository: Option<(Value, Option<Value>)>,
    output: Option<Value>,
    inputs: Option<Map<String, Value>>,
    environment: Option<Value>,
    /// Whether the binding stores files (their content is never listed).
    has_files: bool,
}

impl SavedBinding {
    /// A listed contract: `raw` as the service returned it (its inputs are
    /// kept unchanged), `projected` its checked [`project_listed_contract`]
    /// projection, from which the other settings are rebuilt.
    fn of(raw: &Value, projected: &Value) -> Self {
        let configuration = &projected["run_configuration"];
        let text = |value: &Value| value.as_str().map(|text| json!(text));
        let repository = configuration["context_repositories"]
            .as_array()
            .and_then(|repositories| repositories.first())
            .and_then(|repository| Some((text(&repository["url"])?, text(&repository["branch"]))));
        let output = configuration["output"].as_object().map(|output| {
            let mut kept = Map::new();
            for key in ["type", "repository", "branch"] {
                if let Some(value) = output.get(key).and_then(text) {
                    kept.insert(key.into(), value);
                }
            }
            Value::Object(kept)
        });
        Self {
            model: text(&projected["model"]),
            effort: text(&configuration["reasoning_effort"]),
            repository,
            output,
            inputs: raw["inputs"].as_object().cloned(),
            environment: text(&configuration["environment"]),
            has_files: configuration["stored_files"]
                .as_array()
                .is_some_and(|files| !files.is_empty()),
        }
    }

    fn output_repository(&self) -> Option<&str> {
        self.output.as_ref()?["repository"].as_str()
    }
}

fn contract_attach(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let reference = contract_reference(context)?;
    let settings_given = BindingOptions::any(context);
    let options = BindingOptions::parse(context)?;
    let plan = context.invocation.preview || !context.invocation.yes;
    // Request 0: binding settings apply to webhook jobs only, and a plan's
    // quote prices the job's type.
    let mut live = false;
    let mut job_type = None;
    if settings_given || plan {
        let body = get_json(context, 0, &format!("/triggers/{}", encode_segment(&id)))?;
        let detail = project_detail(&body, false, &context.environment)?;
        job_type = detail["job"]["type"]
            .as_str()
            .filter(|kind| !kind.is_empty())
            .map(str::to_owned);
        let kind = job_type.as_deref().unwrap_or_default();
        if settings_given && kind != "webhook" {
            let error = invalid(format!(
                "job {} is a {} job; run settings (--model, --reasoning-effort, --repo, --commit-output, --input, --inputs-file, --environment, --file, --replace and the --clear options) apply to webhook jobs only",
                human_safe_scalar(&id),
                human_safe_scalar(kind)
            ));
            let argv = argv_without_binding_options(&context.invocation.argv);
            return Err(context.corrected(
                error,
                "Attach without those options: `{command}`",
                argv,
            ));
        }
        live = detail["status"]["delivery_mode"] == "live";
    }
    // Request 1: the bound programs and their saved settings.
    let path = format!("/triggers/{}/contracts", encode_segment(&id));
    let listing = get_json(context, 1, &path)?;
    let contracts = listed_contracts(&listing)?;
    let projected = contracts
        .iter()
        .enumerate()
        .map(|(index, contract)| project_listed_contract(contract, index))
        .collect::<Result<Vec<_>, _>>()?;
    let merging = contracts
        .iter()
        .any(|contract| contract.get("environment").is_some());
    // --replace onto another program that is already bound would reset that
    // binding's settings.
    if let Some(replaced) = options
        .replace
        .as_deref()
        .filter(|replaced| *replaced != reference)
    {
        if contracts
            .iter()
            .any(|contract| contract["program_ref"].as_str() == Some(reference.as_str()))
        {
            let error = invalid(format!(
                "{reference} is already bound to job {}; --replace would reset its settings. Change it in place without --replace, or detach {replaced} first",
                human_safe_scalar(&id)
            ));
            let argv = argv_without(&context.invocation.argv, &["--replace"], &[]);
            return Err(context.corrected(
                error,
                "Change it in place without --replace: `{command}`",
                argv,
            ));
        }
    }
    let base = options.replace.as_deref().unwrap_or(&reference);
    let saved = contracts
        .iter()
        .zip(&projected)
        .find(|(_, contract)| contract["program_ref"].as_str() == Some(base))
        .map(|(raw, contract)| SavedBinding::of(raw, contract));
    let mut body = Map::new();
    body.insert("program_ref".into(), json!(reference));
    // A --replace of the attached ref itself is an ordinary re-attach.
    let moved = options
        .replace
        .as_deref()
        .is_some_and(|replaced| replaced != reference);
    let client_merge = saved.is_some() && (moved || !merging);
    match &saved {
        // A merging service keeps what it saved: send the same ref as
        // replace_program_ref and only the changes.
        Some(saved) if !client_merge => {
            body.insert("replace_program_ref".into(), json!(reference));
            server_merge(&options, saved, &mut body)?;
        }
        Some(saved) => client_merge_body(&options, saved, moved, &mut body)?,
        None => {
            if let Some(commit) = &options.commit {
                if options.repo.is_none() {
                    return Err(runs::commit_output_mismatch(commit));
                }
            }
            fresh_body(&options, &mut body);
        }
    }
    if let Some(replace) = &options.replace {
        body.insert("replace_program_ref".into(), json!(replace));
    }
    let body = Value::Object(body);
    let bytes = canonical(&body).into_bytes();
    let mut planned = context.planned(2, &path, &[], Some(&bytes));
    if plan {
        // Advisory: a failed quote leaves the plan without one.
        let server_saved = saved.as_ref().filter(|_| !client_merge);
        let inputs = effective_hold(&body, server_saved, job_type.as_deref());
        let request = quote_request(context, 3, &inputs);
        match send_quote(context, &request) {
            Ok(quote) => planned["quote"] = quote,
            Err(error) if error.code == ErrorCode::Cancelled => return Err(error),
            Err(_) => {}
        }
    }
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let mut request = Request::from_manifest(context.operation, 2, path);
    request.body = Some(bytes);
    let response = match context.send(&request) {
        Err(error) if live && immutable_refusal(&error) => {
            return Err(live_refusal(context, &id, error));
        }
        other => other?.json_object()?,
    };
    if response.get("bound").and_then(Value::as_str) != Some(reference.as_str()) {
        return Err(protocol("bound"));
    }
    if context.mode == crate::OutputMode::Human {
        if client_merge && !merging {
            let _ = context.err.write_all(OLDER_SERVICE_NOTE.as_bytes());
        } else if moved
            && options.files.is_none()
            && !options.clear_files
            && saved.as_ref().is_some_and(|saved| saved.has_files)
        {
            let _ = context.err.write_all(FILES_NOT_CARRIED_NOTE.as_bytes());
        }
    }
    let mut text = format!(
        "Attached {} to job {}.\n",
        human_safe_scalar(&reference),
        human_safe_scalar(&id),
    );
    if let Some(line) = settings_line(&body) {
        let _ = writeln!(text, "  settings: {line}");
    }
    let _ = writeln!(
        text,
        "List the job's contracts with `cli job contract list {}`.",
        human_safe_scalar(&id)
    );
    context.human = Some(text);
    Ok(json!({"contracts": [{"program_ref": reference}]}))
}

const OLDER_SERVICE_NOTE: &str =
    "note: this service does not report stored files or environment; re-attaching may drop them\n";
const FILES_NOT_CARRIED_NOTE: &str =
    "note: stored files are not carried to the new revision; pass --file to attach them\n";

/// Job types whose runs always read a repository.
const REPOSITORY_JOB_TYPES: [&str; 3] = [
    "github-issue-opened",
    "github-pull-request-opened",
    "github-release-published",
];

/// The hold inputs of an attach plan: the binding as it will run. `body` is
/// the request body; on the server-merge path (`saved`) a setting the body
/// leaves out keeps its saved value and a JSON null clears it.
fn effective_hold<'a>(
    body: &'a Value,
    saved: Option<&'a SavedBinding>,
    job_type: Option<&'a str>,
) -> HoldInputs<'a> {
    let effective = |key: &str, saved_value: Option<&'a Value>| match body.get(key) {
        Some(value) => value.as_str(),
        None => saved_value.and_then(Value::as_str),
    };
    let saved_repository = saved
        .and_then(|saved| saved.repository.as_ref())
        .map(|(url, _)| url);
    HoldInputs {
        program_ref: body["program_ref"].as_str(),
        model: effective("model", saved.and_then(|saved| saved.model.as_ref())),
        reasoning_effort: effective(
            "reasoning_effort",
            saved.and_then(|saved| saved.effort.as_ref()),
        ),
        environment: effective(
            "environment",
            saved.and_then(|saved| saved.environment.as_ref()),
        ),
        repositories_bound: effective("repository_url", saved_repository).is_some()
            || job_type.is_some_and(|kind| REPOSITORY_JOB_TYPES.contains(&kind)),
        job_type,
    }
}

/// `--commit-output` must name the repository the runs read: the `--repo`
/// given, or the saved one when it is kept. Returns that repository's URL.
fn check_commit_output(
    options: &BindingOptions,
    saved: &SavedBinding,
) -> Result<Option<String>, RunnerError> {
    let Some(commit) = &options.commit else {
        return Ok(None);
    };
    let effective = match &options.repo {
        Some(repo) => Some(repo.url()),
        None if options.clear_repo => None,
        None => saved
            .repository
            .as_ref()
            .and_then(|(url, _)| url.as_str().map(str::to_owned)),
    };
    match effective {
        Some(url) if same_url(&url, &commit.url()) => Ok(Some(url)),
        _ => Err(runs::commit_output_mismatch(commit)),
    }
}

/// Whether a `--repo` leaves the saved commit output pointing elsewhere.
fn output_elsewhere(options: &BindingOptions, saved: &SavedBinding) -> bool {
    match (&options.repo, saved.output_repository()) {
        (Some(repo), Some(output)) => !same_url(&repo.url(), output),
        _ => false,
    }
}

/// The server-merge body: only the fields the options change, with JSON
/// nulls for clears.
fn server_merge(
    options: &BindingOptions,
    saved: &SavedBinding,
    body: &mut Map<String, Value>,
) -> Result<(), RunnerError> {
    let commit_url = check_commit_output(options, saved)?;
    if let Some(model) = &options.model {
        body.insert("model".into(), json!(model));
    }
    if let Some(effort) = &options.effort {
        body.insert("reasoning_effort".into(), json!(effort));
    }
    if let Some(repo) = &options.repo {
        body.insert("repository_url".into(), json!(repo.url()));
        body.insert("repository_branch".into(), json!(repo.branch));
        if output_elsewhere(options, saved) {
            body.insert("output".into(), Value::Null);
        }
    }
    if options.clear_repo {
        body.insert("repository_url".into(), Value::Null);
        body.insert("repository_branch".into(), Value::Null);
        body.insert("output".into(), Value::Null);
    }
    if options.clear_commit {
        body.insert("output".into(), Value::Null);
    }
    if let (Some(commit), Some(url)) = (&options.commit, &commit_url) {
        body.insert("output".into(), commit_output(url, commit));
    }
    if let Some(inputs) = options.merged_inputs(saved.inputs.as_ref()) {
        body.insert("inputs".into(), Value::Object(inputs));
    }
    if let Some(environment) = &options.environment {
        body.insert("environment".into(), json!(environment));
    }
    options.put_files(body, true);
    Ok(())
}

/// The client-merge body: the saved settings with the options applied, in
/// full (a service that replaces the whole binding on a re-post, or a move
/// to another revision, which also carries the saved `environment`).
fn client_merge_body(
    options: &BindingOptions,
    saved: &SavedBinding,
    moved: bool,
    body: &mut Map<String, Value>,
) -> Result<(), RunnerError> {
    let commit_url = check_commit_output(options, saved)?;
    let mut model = saved.model.clone();
    let mut effort = saved.effort.clone();
    let mut repository = saved.repository.clone();
    let mut output = saved.output.clone();
    if let Some(value) = &options.model {
        model = Some(json!(value));
    }
    if let Some(value) = &options.effort {
        effort = Some(json!(value));
    }
    if let Some(repo) = &options.repo {
        repository = Some((json!(repo.url()), repo.branch.as_ref().map(|b| json!(b))));
        if output_elsewhere(options, saved) {
            output = None;
        }
    }
    if options.clear_repo {
        repository = None;
        output = None;
    }
    if options.clear_commit {
        output = None;
    }
    if let (Some(commit), Some(url)) = (&options.commit, &commit_url) {
        output = Some(commit_output(url, commit));
    }
    let inputs = options
        .merged_inputs(saved.inputs.as_ref())
        .or_else(|| saved.inputs.clone());
    if let Some(model) = model {
        body.insert("model".into(), model);
    }
    if let Some(effort) = effort {
        body.insert("reasoning_effort".into(), effort);
    }
    if let Some((url, branch)) = repository {
        body.insert("repository_url".into(), url);
        if let Some(branch) = branch {
            body.insert("repository_branch".into(), branch);
        }
    }
    if let Some(output) = output {
        body.insert("output".into(), output);
    }
    if let Some(inputs) = inputs.filter(|inputs| !inputs.is_empty()) {
        body.insert("inputs".into(), Value::Object(inputs));
    }
    if let Some(environment) = &options.environment {
        body.insert("environment".into(), json!(environment));
    } else if let Some(environment) = saved.environment.as_ref().filter(|_| moved) {
        body.insert("environment".into(), environment.clone());
    }
    options.put_files(body, true);
    Ok(())
}

/// The body for a program that is not bound yet: only the options given,
/// without nulls.
fn fresh_body(options: &BindingOptions, body: &mut Map<String, Value>) {
    if let Some(model) = &options.model {
        body.insert("model".into(), json!(model));
    }
    if let Some(effort) = &options.effort {
        body.insert("reasoning_effort".into(), json!(effort));
    }
    if let Some(repo) = &options.repo {
        body.insert("repository_url".into(), json!(repo.url()));
        if let Some(branch) = &repo.branch {
            body.insert("repository_branch".into(), json!(branch));
        }
    }
    if let (Some(commit), Some(repo)) = (&options.commit, &options.repo) {
        body.insert("output".into(), commit_output(&repo.url(), commit));
    }
    if let Some(inputs) = options
        .merged_inputs(None)
        .filter(|inputs| !inputs.is_empty())
    {
        body.insert("inputs".into(), Value::Object(inputs));
    }
    if let Some(environment) = &options.environment {
        body.insert("environment".into(), json!(environment));
    }
    options.put_files(body, false);
}

/// The service's refusal to change a live webhook's binding.
fn immutable_refusal(error: &RunnerError) -> bool {
    error.code == ErrorCode::ServiceRequestRejected
        && error
            .details
            .as_ref()
            .and_then(|details| details.get("serviceMessage"))
            .and_then(Value::as_str)
            .is_some_and(|message| message.to_ascii_lowercase().contains("immutable"))
}

/// A live webhook's refusal to change its binding: also name the switch to
/// test delivery, with the spec on standard input.
fn live_refusal(context: &Context<'_>, id: &str, error: RunnerError) -> RunnerError {
    let words = ["job", "update", id, "--spec-file", "-", "--yes"];
    let mut error = error;
    error.action = format!(
        "{} The job delivers live; to change its binding, switch it to test delivery first: `{}` with details.suggestedStdin on standard input.",
        error.action,
        context.command(&words.join(" "))
    );
    error
        .with_detail("suggestedArgv", json!(context.follow_up_argv(&words)))
        .with_detail(
            "suggestedStdin",
            Value::String("{\"delivery_mode\":\"test\"}\n".into()),
        )
}

/// `reasoning effort E; repository URL@BRANCH; commit output URL; inputs
/// k1, k2`: the parts present in a binding body (service names) or a
/// projected `run_configuration`; input values are never shown.
fn settings_line(settings: &Value) -> Option<String> {
    let mut parts = Vec::new();
    if let Some(effort) = settings["reasoning_effort"].as_str() {
        parts.push(format!("reasoning effort {effort}"));
    }
    let repository = settings["repository_url"]
        .as_str()
        .map(|url| (url, settings["repository_branch"].as_str()))
        .or_else(|| {
            let first = &settings["context_repositories"][0];
            first["url"]
                .as_str()
                .map(|url| (url, first["branch"].as_str()))
        });
    if let Some((url, branch)) = repository {
        parts.push(match branch {
            Some(branch) => format!("repository {url}@{branch}"),
            None => format!("repository {url}"),
        });
    }
    if let Some(url) = settings["output"]["repository"].as_str() {
        parts.push(format!("commit output {url}"));
    }
    let mut keys = settings["inputs"]
        .as_object()
        .map(|inputs| inputs.keys().map(String::as_str).collect::<Vec<_>>())
        .unwrap_or_default();
    if let Some(entries) = settings["input_entries"].as_array() {
        keys.extend(entries.iter().filter_map(|entry| entry["name"].as_str()));
    }
    if !keys.is_empty() {
        keys.sort_unstable();
        parts.push(format!("inputs {}", keys.join(", ")));
    }
    let mut files = settings["files"]
        .as_object()
        .map(|files| files.keys().map(String::as_str).collect::<Vec<_>>())
        .unwrap_or_default();
    if let Some(stored) = settings["stored_files"].as_array() {
        files.extend(stored.iter().filter_map(|file| file["name"].as_str()));
    }
    if !files.is_empty() {
        files.sort_unstable();
        parts.push(format!("files {}", files.join(", ")));
    }
    (!parts.is_empty()).then(|| human_safe_scalar(&parts.join("; ")))
}

// ------------------------------------------------------------------- human

fn text_or_dash(value: &Value) -> String {
    match value {
        Value::String(text) if !text.is_empty() => human_safe_scalar(text),
        Value::Number(number) => number.to_string(),
        _ => "-".into(),
    }
}

/// 9999-12-31T23:59:59.999Z in epoch milliseconds: later times print raw.
const MAX_ISO_MS: i64 = 253_402_300_799_999;

/// Epoch milliseconds as an RFC 3339 UTC time with milliseconds, as
/// JavaScript's `toISOString`; anything else is `-`.
fn iso_or_dash(value: &Value) -> String {
    match value.as_i64() {
        Some(ms) if (0..=MAX_ISO_MS).contains(&ms) => {
            iso_ms(value).unwrap_or_else(|| ms.to_string())
        }
        Some(ms) => ms.to_string(),
        None => "-".into(),
    }
}

fn bool_or_dash(value: &Value) -> String {
    value
        .as_bool()
        .map_or_else(|| "-".into(), |flag| flag.to_string())
}

/// Readable `job show|create|update|configure` output: one `label: value`
/// line per known field in a fixed order, times as RFC 3339, the
/// absolute endpoint URL and, on create, the once-only signing secret; then
/// copyable `Next:` commands in this environment.
fn human_detail(result: &Value) -> String {
    let job = &result["job"];
    let status = &result["status"];
    let mut text = String::new();
    let id = text_or_dash(&job["id"]);
    let kind = text_or_dash(&job["type"]);
    let _ = writeln!(text, "Job {id} ({kind})");
    let mut line = |label: &str, value: String| {
        if value != "-" {
            let _ = writeln!(text, "  {label}: {value}");
        }
    };
    line("name", text_or_dash(&job["name"]));
    line("created", iso_or_dash(&job["created_at"]));
    line("active", bool_or_dash(&status["active"]));
    let interval = if status["interval_seconds"].is_null() {
        &job["interval_seconds"]
    } else {
        &status["interval_seconds"]
    };
    line(
        "interval",
        match interval.as_i64() {
            Some(seconds) => format!("{seconds}s"),
            None => "-".into(),
        },
    );
    line("next fire", iso_or_dash(&status["next_fire_at"]));
    line("last fired", iso_or_dash(&status["last_fired_at"]));
    line("delivery mode", text_or_dash(&status["delivery_mode"]));
    line("last event", iso_or_dash(&status["last_event_at"]));
    line("secret rotated", iso_or_dash(&status["secret_rotated_at"]));
    let last_run = if status["last_run_id"].is_null() {
        &job["last_run_id"]
    } else {
        &status["last_run_id"]
    };
    line("last run", text_or_dash(last_run));
    let last_error = if status["last_error"].is_null() {
        &job["last_error"]
    } else {
        &status["last_error"]
    };
    line("last error", text_or_dash(last_error));
    if let Some(counts) = status["counts"].as_object() {
        line("runs", run_counts(counts));
    }
    let contracts = status["contracts"]
        .as_array()
        .or_else(|| job["contracts"].as_array());
    match contracts {
        Some(contracts) => {
            for contract in contracts {
                line(
                    "program",
                    format!(
                        "{} enabled={} model={}",
                        text_or_dash(&contract["program_ref"]),
                        bool_or_dash(&contract["enabled"]),
                        text_or_dash(&contract["model"])
                    ),
                );
            }
        }
        None => line("program", text_or_dash(&job["program_ref"])),
    }
    line("endpoint URL", text_or_dash(&result["endpoint_url"]));
    line("signing secret", text_or_dash(&result["signing_secret"]));
    let raw_id = job["id"].as_str().unwrap_or_default();
    match kind.as_str() {
        "schedule" => text.push_str(&next_line(&[
            "job",
            "configure",
            raw_id,
            "--interval-seconds",
            "N",
            "--yes",
        ])),
        "webhook" => {
            text.push_str(&next_line(&["job", "deliveries", raw_id]));
            text.push_str(&next_line(&["job", "contract", "list", raw_id]));
        }
        _ => {}
    }
    if let Some(run) = last_run.as_str() {
        text.push_str(&next_line(&["run", "show", run]));
    }
    text
}

/// Readable `job rotate-secret` output.
fn human_rotated(result: &Value) -> String {
    let id = result["id"].as_str().unwrap_or_default();
    let mut text = format!(
        "Rotated the signing secret of job {}\n",
        human_safe_scalar(id)
    );
    for (label, field) in [
        ("endpoint URL", "endpoint_url"),
        ("signing secret", "signing_secret"),
    ] {
        let value = text_or_dash(&result[field]);
        if value != "-" {
            let _ = writeln!(text, "  {label}: {value}");
        }
    }
    text.push_str(&next_line(&["job", "deliveries", id]));
    text
}

fn human_list(result: &Value) -> String {
    let jobs = result["jobs"].as_array().map_or(&[][..], Vec::as_slice);
    let mut text = String::new();
    text.push_str(&jobs_line(jobs.len(), &result["job_limit"]));
    if jobs.is_empty() {
        text.push_str("No jobs.\n");
    }
    for job in jobs {
        let _ = writeln!(
            text,
            "{} {} name={} program={} interval={} next={}",
            text_or_dash(&job["id"]),
            text_or_dash(&job["type"]),
            text_or_dash(&job["name"]),
            text_or_dash(&job["program_ref"]),
            text_or_dash(&job["interval_seconds"]),
            iso_or_dash(&job["next_fire_at"]),
        );
    }
    let types = result["types"]
        .as_array()
        .map(|types| {
            types
                .iter()
                .map(|kind| text_or_dash(&kind["id"]))
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let _ = writeln!(
        text,
        "Types: {}",
        if types.is_empty() { "-".into() } else { types }
    );
    text
}

fn human_deliveries(result: &Value) -> String {
    let deliveries = result["deliveries"]
        .as_array()
        .map_or(&[][..], Vec::as_slice);
    if deliveries.is_empty() {
        return "No deliveries.\n".into();
    }
    deliveries.iter().fold(String::new(), |mut text, delivery| {
        let _ = writeln!(
            text,
            "{} {} {} test={} runs={}",
            text_or_dash(&delivery["id"]),
            iso_or_dash(&delivery["received_at"]),
            text_or_dash(&delivery["outcome"]),
            delivery["test_only"],
            human_safe_scalar(&canonical(&delivery["runs"])),
        );
        text
    })
}

fn human_contracts(result: &Value) -> String {
    let contracts = result["contracts"]
        .as_array()
        .map_or(&[][..], Vec::as_slice);
    if contracts.is_empty() {
        return "No contracts.\n".into();
    }
    contracts.iter().fold(String::new(), |mut text, contract| {
        let _ = writeln!(
            text,
            "{} enabled={} model={}",
            text_or_dash(&contract["program_ref"]),
            contract["enabled"]
                .as_bool()
                .map_or("-", |flag| if flag { "true" } else { "false" }),
            text_or_dash(&contract["model"]),
        );
        if let Some(line) = settings_line(&contract["run_configuration"]) {
            let _ = writeln!(text, "  settings: {line}");
        }
        text
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn listed(contract: &Value) -> Result<Value, RunnerError> {
        project_listed_contract(contract, 0)
    }

    #[test]
    fn integers_accept_zero_fraction_floats_only() {
        assert_eq!(as_integer(&json!(86_400)), Some(86_400));
        assert_eq!(as_integer(&json!(86_400.0)), Some(86_400));
        assert_eq!(as_integer(&json!(60.5)), None);
        assert_eq!(as_integer(&json!("60")), None);
        assert_eq!(as_integer(&json!(1e300)), None);
    }

    #[test]
    fn identifiers_follow_the_result_schema() {
        assert!(is_uuid("3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10"));
        assert!(!is_uuid("8F40229E-204F-4153-9CCC-AFE367567D52"));
        assert!(is_run_id("run_abc-1_2"));
        assert!(!is_run_id("run_"));
        assert!(is_model_id("gpt-5.4-mini"));
        assert!(is_program_ref("exowner1/slack-hello@97ddf3a447f534a7"));
        assert!(!is_program_ref("exowner1/slack-hello"));
    }

    #[test]
    fn job_projection_keeps_public_fields_only_and_sanitizes_prose() {
        let projected = project_job(&json!({
            "id": "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10",
            "type": "webhook",
            "createdAt": 1,
            "internalRef": "opaque-1",
            "adopted": true,
            "contextRepositoryId": 7,
            "contextRepository": {"id": 1},
            "lastError": "line\nbreak",
            "name": null,
        }))
        .unwrap();
        assert_eq!(
            projected,
            json!({
                "id": "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10",
                "type": "webhook",
                "created_at": 1,
                "created_at_iso": "1970-01-01T00:00:00.001Z",
                "last_error": "line break",
                "name": null,
            })
        );
        assert!(project_job(&json!({"id": "x", "type": "webhook", "createdAt": 1})).is_err());
        assert!(
            project_job(&json!({"id": "3c1a9e57-0b4d-4f2a-9e61-5d7b2c8a4f10", "type": "webhook"}))
                .is_err()
        );
    }

    #[test]
    fn job_limit_follows_the_entitlement_not_the_legacy_ceiling() {
        let limit = |body: Value| project_job_limit(body.as_object().unwrap());
        assert_eq!(
            limit(
                json!({"max_triggers": 5, "trigger_limit": {"kind": "limited", "max": 3, "x": 1}})
            )
            .unwrap(),
            json!({"kind": "limited", "max": 3})
        );
        assert_eq!(
            limit(json!({"max_triggers": 5, "trigger_limit": {"kind": "unlimited", "max": 9}}))
                .unwrap(),
            json!({"kind": "unlimited"})
        );
        for kind in ["unavailable", "metered", ""] {
            assert_eq!(
                limit(json!({"max_triggers": 5, "trigger_limit": {"kind": kind}})).unwrap(),
                json!({"kind": "unavailable"})
            );
        }
        assert_eq!(
            limit(json!({"max_triggers": 5})).unwrap(),
            json!({"kind": "limited", "max": 5})
        );
        assert_eq!(
            limit(json!({"max_triggers": 5, "trigger_limit": null})).unwrap(),
            json!({"kind": "limited", "max": 5})
        );
        assert_eq!(limit(json!({})).unwrap(), json!({"kind": "unavailable"}));
        assert_eq!(
            limit(json!({"max_triggers": "5"})).unwrap(),
            json!({"kind": "unavailable"})
        );
        for bad in [
            json!({"trigger_limit": {"kind": "limited"}}),
            json!({"trigger_limit": {"kind": "limited", "max": -1}}),
            json!({"trigger_limit": {"kind": "limited", "max": 1.5}}),
            json!({"trigger_limit": {"kind": "limited", "max": "3"}}),
            json!({"trigger_limit": {"max": 3}}),
            json!({"trigger_limit": {"kind": 1}}),
            json!({"trigger_limit": "limited"}),
        ] {
            assert!(limit(bad).is_err());
        }
    }

    #[test]
    fn job_limit_lines_and_max() {
        let limited = json!({"kind": "limited", "max": 3});
        let unlimited = json!({"kind": "unlimited"});
        let unavailable = json!({"kind": "unavailable"});
        assert_eq!(limit_max(&limited), json!(3));
        assert_eq!(limit_max(&unlimited), Value::Null);
        assert_eq!(limit_max(&unavailable), Value::Null);
        assert_eq!(jobs_line(2, &limited), "Jobs: 2 of 3 allowed\n");
        assert_eq!(jobs_line(2, &unlimited), "Jobs: 2 (unlimited)\n");
        assert_eq!(
            jobs_line(2, &unavailable),
            "Jobs: 2 (limit unavailable; try again)\n"
        );
    }

    #[test]
    fn listed_contracts_project_saved_settings_without_content() {
        let projected = listed(&json!({
            "program_ref": "exowner1/hello@0123456789abcdef",
            "owner": "exowner1",
            "slug": "hello",
            "rev_id": "0123456789abcdef",
            "content": "program text",
            "is_platform_default": false,
            "enabled": true,
            "bound_at": 1,
            "inputs": {"topic": "cats", "limit": 3, "flag": true, "cost_center": "ops"},
            "model": null,
            "effective_model": "model-a",
            "reasoning_effort": null,
            "repositories": [],
            "output": null,
            "environment": null,
            "files": [{"name": "a.md", "size": 2, "content": "hi"}],
        }))
        .unwrap();
        assert_eq!(
            projected["run_configuration"],
            json!({
                "inputs": {"topic": "cats", "limit": "3", "flag": "true"},
                "input_entries": [{"name": "cost_center", "value": "ops"}],
                "stored_files": [{"name": "a.md", "size": 2}],
            })
        );
        assert_eq!(projected["bound_at_iso"], json!("1970-01-01T00:00:00.001Z"));
        assert!(projected.get("content").is_none() && projected.get("owner").is_none());
        let bare = listed(&json!({
            "program_ref": "exowner1/hello@0123456789abcdef", "inputs": {}, "repositories": [],
        }))
        .unwrap();
        assert!(bare.get("run_configuration").is_none());
        let error = listed(&json!({
            "program_ref": "exowner1/hello@0123456789abcdef", "inputs": {"a": {"b": 1}},
        }))
        .unwrap_err();
        assert_eq!(
            error.details.unwrap()["reason"],
            json!("unexpected job response: contracts[0].inputs.a")
        );
    }

    #[test]
    fn settings_line_names_parts_but_never_input_values() {
        assert_eq!(
            settings_line(&json!({
                "program_ref": "x", "reasoning_effort": "low",
                "repository_url": "https://github.com/o/n", "repository_branch": null,
                "output": null, "inputs": {"b": "secret", "a": "secret"},
            }))
            .as_deref(),
            Some("reasoning effort low; repository https://github.com/o/n; inputs a, b")
        );
        assert_eq!(settings_line(&json!({"program_ref": "x"})), None);
        assert_eq!(
            settings_line(&json!({"files": {"b.md": "YQ==", "a.md": "YQ=="}})).as_deref(),
            Some("files a.md, b.md")
        );
        assert_eq!(
            settings_line(&json!({"stored_files": [{"name": "n.md", "size": 1}]})).as_deref(),
            Some("files n.md")
        );
    }

    #[test]
    fn binding_options_are_dropped_from_the_suggested_argv() {
        let argv = [
            "cli",
            "job",
            "contract",
            "attach",
            "J",
            "R",
            "--model",
            "m",
            "--clear-repo",
            "--input=a=b",
            "--yes",
            "--",
            "--model",
        ]
        .map(String::from);
        assert_eq!(
            argv_without_binding_options(&argv),
            [
                "cli", "job", "contract", "attach", "J", "R", "--yes", "--", "--model"
            ]
        );
    }

    #[test]
    fn server_merge_sends_changes_and_client_merge_sends_everything() {
        let options = BindingOptions {
            model: None,
            effort: None,
            repo: Some(runs::parse_repository("--repo", "o/other").unwrap()),
            commit: None,
            clear_repo: false,
            clear_commit: false,
            inputs: Some(BTreeMap::from([("t".to_owned(), "v".to_owned())])),
            clear_inputs: vec!["n".to_owned()],
            environment: None,
            replace: None,
            files: None,
            clear_files: false,
        };
        let saved = SavedBinding {
            model: Some(json!("model-a")),
            effort: None,
            repository: Some((json!("https://github.com/o/app"), Some(json!("main")))),
            output: Some(json!({"type": "commit", "repository": "https://github.com/o/app"})),
            inputs: Some(json!({"n": 1, "k": 2}).as_object().unwrap().clone()),
            environment: Some(json!("builtin")),
            has_files: true,
        };
        let mut body = Map::new();
        server_merge(&options, &saved, &mut body).unwrap();
        assert_eq!(
            Value::Object(body),
            json!({"repository_url": "https://github.com/o/other", "repository_branch": null,
                   "output": null, "inputs": {"k": 2, "t": "v"}})
        );
        let mut body = Map::new();
        client_merge_body(&options, &saved, false, &mut body).unwrap();
        assert_eq!(
            Value::Object(body),
            json!({"model": "model-a", "repository_url": "https://github.com/o/other",
                   "inputs": {"k": 2, "t": "v"}})
        );
        // A move to another revision also carries the saved environment, and
        // the commit output names the effective repository's URL.
        let moved = BindingOptions {
            repo: None,
            commit: Some(runs::parse_repository("--commit-output", "O/APP@out").unwrap()),
            ..options
        };
        let mut body = Map::new();
        client_merge_body(&moved, &saved, true, &mut body).unwrap();
        assert_eq!(body["environment"], json!("builtin"));
        assert_eq!(
            body["output"],
            json!({"type": "commit", "repository": "https://github.com/o/app", "branch": "out"})
        );
    }

    #[test]
    fn plan_quote_prices_the_binding_as_it_will_run() {
        let saved = SavedBinding {
            model: Some(json!("model-a")),
            effort: Some(json!("low")),
            repository: Some((json!("https://github.com/o/app"), None)),
            environment: Some(json!("builtin")),
            ..SavedBinding::default()
        };
        let body = json!({"program_ref": "o/p@0123456789abcdef", "reasoning_effort": "high",
                          "repository_url": null, "repository_branch": null});
        let merged = effective_hold(&body, Some(&saved), Some("webhook"));
        assert_eq!(
            (merged.model, merged.reasoning_effort, merged.environment),
            (Some("model-a"), Some("high"), Some("builtin"))
        );
        assert!(!merged.repositories_bound);
        let fresh = effective_hold(&body, None, Some("github-issue-opened"));
        assert_eq!((fresh.model, fresh.environment), (None, None));
        assert!(fresh.repositories_bound);
    }

    #[test]
    fn run_counts_fold_into_public_states() {
        let status = project_status(&json!({
            "counts": {"pending": 1, "claimed": 2, "completed": 3, "ambiguous": 1,
                       "billing_pending": 2, "pending_funds": 1, "cost_units": 9, "other": 4},
            "configurationToken": "a".repeat(64),
        }))
        .unwrap();
        assert_eq!(
            status["counts"],
            json!({"queued": 1, "running": 2, "completed": 3, "failed": 1,
                   "cancelled": 0, "awaiting_billing": 3})
        );
        assert_eq!(status["revision_token"], json!("a".repeat(64)));
        assert!(status.get("configuration_token").is_none());
        assert_eq!(
            run_counts(status["counts"].as_object().unwrap()),
            "queued 1, running 2, completed 3, failed 1, awaiting billing settlement 3"
        );
    }
}
