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
//!
//! Mirrors `cli/bun/src/core/service/jobs.ts`.
use super::http::{Request, encode_segment};
use super::program_ref;
use super::render::{absolute_url, add_iso, canonical, iso_ms, next_line, valid_text};
use super::{Context, Environment, Gate};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
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
            let named = missing
                .iter()
                .map(|key| format!("{path}{key}"))
                .collect::<Vec<_>>();
            let named = named.iter().map(String::as_str).collect::<Vec<_>>();
            self.violations
                .push(format!("{label} needs {}{hint}", and_list(&named)));
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
        let (required, label) = match &chosen {
            Some(kind) => {
                let (variant_keys, required) = keys_and_required(&variants[kind]);
                keys.extend(variant_keys);
                (required, format!("a {kind} job spec"))
            }
            // An unknown type: check keys against every type's.
            None => {
                for variant in variants.values() {
                    keys.extend(keys_and_required(variant).0);
                }
                (Vec::new(), "a job spec".to_owned())
            }
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

/// The projected jobs and job limit (`max_jobs`) of a job list body (shared
/// with `cli service triage`).
pub(super) fn project_jobs(body: &Map<String, Value>) -> Result<(Vec<Value>, Value), RunnerError> {
    let jobs = array(body.get("triggers").unwrap_or(&Value::Null), 1000, "jobs")?
        .iter()
        .map(project_job)
        .collect::<Result<Vec<_>, _>>()?;
    let max = match body.get("max_triggers") {
        None | Some(Value::Null) => Value::Null,
        Some(value) => scalar(value, Kind::EpochMs, "max_jobs")?,
    };
    Ok((jobs, max))
}

fn list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let body = get_json(context, 0, "/triggers")?;
    let (jobs, max) = project_jobs(&body)?;
    let limit = object(
        body.get("trigger_limit").unwrap_or(&Value::Null),
        "job_limit",
    )?;
    let mut job_limit = Map::new();
    copy(
        &mut job_limit,
        limit,
        "job_limit",
        &[
            ("kind", Kind::Text(64), false),
            ("limit", Kind::EpochMs, false),
        ],
    )?;
    if !job_limit.contains_key("kind") {
        return Err(protocol("job_limit.kind"));
    }
    let types = array(body.get("types").unwrap_or(&Value::Null), 64, "types")?
        .iter()
        .map(project_type)
        .collect::<Result<Vec<_>, _>>()?;
    let result = json!({
        "jobs": jobs,
        "max_jobs": max,
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
            let pinned = program_ref::resolve_to_run(context, &mut reference, 2, 3, None, false)?;
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
        planned["quote"] = quote(context)?;
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

/// The anonymous `GET /run/quote` hold for the confirmation plan (the
/// default environment; the job's own environment is not sent). The price
/// policy reference stays internal.
fn quote(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let body = get_json(context, 0, "/run/quote")?;
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
    // The contract route names differ from the job record's; both project
    // to the same public contract fields.
    let mut contracts = Vec::new();
    for contract in array(
        body.get("contracts").unwrap_or(&Value::Null),
        16,
        "contracts",
    )? {
        let source = object(contract, "contracts")?;
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
        contracts.push(project_contract(&Value::Object(renamed), "contracts")?);
    }
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

fn contract_change(context: &mut Context<'_>, attach: bool) -> Result<Value, RunnerError> {
    let id = job_id(context)?;
    let value = context.argument("OWNER/SLUG@REV").unwrap_or_default();
    let reference = program_ref::parse(value, true)
        .map_err(|error| error.with_detail("reason", unpinned_reason(context, value)))?
        .pinned()
        .unwrap_or_default();
    if attach && context.option("--model").is_some() {
        return Err(invalid(
            "the service does not accept --model when attaching a contract; attached contracts run on the service's default job model (see `cli job contract list`)",
        ));
    }
    let (path, body) = if attach {
        let body = serde_json::to_vec(&json!({"program_ref": reference})).expect("JSON serializes");
        (
            format!("/triggers/{}/contracts", encode_segment(&id)),
            Some(body),
        )
    } else {
        (
            format!(
                "/triggers/{}/contracts/{}",
                encode_segment(&id),
                encode_segment(&reference)
            ),
            None,
        )
    };
    if let Gate::Preview(result) = context.gate(context.planned(0, &path, &[], body.as_deref()))? {
        return Ok(result);
    }
    let mut request = Request::from_manifest(context.operation, 0, path);
    request.body = body;
    let response = context.send(&request)?.json_object()?;
    let key = if attach { "bound" } else { "unbound" };
    if response.get(key).and_then(Value::as_str) != Some(reference.as_str()) {
        return Err(protocol(key));
    }
    context.human = Some(format!(
        "{} {} {} job {}.\nList the job's contracts with `cli job contract list {}`.\n",
        if attach { "Attached" } else { "Detached" },
        human_safe_scalar(&reference),
        if attach { "to" } else { "from" },
        human_safe_scalar(&id),
        human_safe_scalar(&id),
    ));
    Ok(json!({"contracts": [{"program_ref": reference}]}))
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
    let limit = match &result["max_jobs"] {
        Value::Number(max) => format!(" of {max} allowed"),
        _ => String::new(),
    };
    let _ = writeln!(text, "Jobs: {}{limit}", jobs.len());
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
        text
    })
}

#[cfg(test)]
mod tests {
    use super::*;

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
