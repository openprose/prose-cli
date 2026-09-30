//! Envelopes, stream lines, planned requests and human rendering for service
//! service operations, plus the shared text validator and redaction.
use super::Environment;
use crate::error::human_safe_scalar;
use crate::output::CommandOutcome;
use crate::{ErrorCode, OutputMode, RunnerError};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fmt::Write as _;

/// Values a handler sets besides its result.
#[derive(Debug, Default)]
pub struct Extras {
    pub next_before: Option<String>,
    pub run_id: Option<String>,
    pub human: Option<String>,
    /// Words after `cli` that fetch the next page (the operation's command
    /// and the options the invocation gave, without `--before`); the human
    /// `Next page:` line appends `--before CURSOR`.
    pub page_words: Vec<String>,
}

/// The shared text validator: non-empty, at most `max` code points,
/// no C0 control or DEL characters.
pub fn valid_text(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.chars().count() <= max
        && !value
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
}

/// Replaces every API-key-shaped token (`rr_test_…`) and the known credential
/// with `[REDACTED]`.
pub fn redact(value: &str, credential: Option<&str>) -> String {
    let mut text = match credential.filter(|credential| !credential.is_empty()) {
        Some(credential) => value.replace(credential, "[REDACTED]"),
        None => value.to_owned(),
    };
    let marker = "rr_test_";
    let mut output = String::with_capacity(text.len());
    while let Some(start) = text.find(marker) {
        output.push_str(&text[..start]);
        output.push_str("[REDACTED]");
        let rest = &text[start + marker.len()..];
        let end = rest
            .char_indices()
            .find(|(_, character)| !character.is_ascii_alphanumeric())
            .map_or(rest.len(), |(index, _)| index);
        text = rest[end..].to_owned();
    }
    output.push_str(&text);
    output
}

/// `details.serviceMessage`: control characters become spaces, keys are
/// redacted, the text is cut to 512 code points; blank text is dropped.
pub fn sanitize_service_message(value: &str, credential: Option<&str>) -> Option<String> {
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
    let redacted = redact(&spaced, credential);
    let cut = redacted.chars().take(512).collect::<String>();
    (!cut.trim().is_empty()).then_some(cut)
}

/// Redacts credential material from every string in an error's details.
pub fn redact_error(error: &mut RunnerError, credential: Option<&str>) {
    fn walk(value: &mut Value, credential: Option<&str>) {
        match value {
            Value::String(text) => *text = redact(text, credential),
            Value::Array(values) => values.iter_mut().for_each(|value| walk(value, credential)),
            Value::Object(object) => object
                .values_mut()
                .for_each(|value| walk(value, credential)),
            _ => {}
        }
    }
    if let Some(details) = error.details.as_mut() {
        for value in details.values_mut() {
            walk(value, credential);
        }
    }
}

/// `details.plannedRequest` / the `--preview` result for one request: the
/// method, what it does in words (`description`), the body digest and the
/// non-secret `summary` of the caller's inputs. The service route and query
/// stay internal.
pub fn planned_request(
    operation: &Value,
    template: &Value,
    _path: &str,
    _query: &[(String, String)],
    body: Option<&[u8]>,
) -> Value {
    let mut planned = json!({
        "operation": operation["id"],
        "method": template["method"],
        "description": operation_sentence(operation),
        "bodySha256": body.map(|bytes| format!("{:x}", Sha256::digest(bytes))),
        "bodyBytes": body.map(<[u8]>::len),
        "effect": operation["effect"],
    });
    if let Some(summary) = body_summary(body) {
        planned["summary"] = summary;
    }
    planned
}

/// The first sentence of an operation's summary, ending in a period.
fn operation_sentence(operation: &Value) -> String {
    let first = operation["summary"]
        .as_str()
        .unwrap_or_default()
        .split('\n')
        .next()
        .unwrap_or_default();
    let sentence = first
        .split(". ")
        .next()
        .unwrap_or_default()
        .trim_end_matches('.');
    format!("{sentence}.")
}

/// The run error words (`shared/fixtures/service/run-errors.v1.json`).
const RUN_ERRORS_TEXT: &str =
    include_str!("../../../../../shared/fixtures/service/run-errors.v1.json");

fn run_errors() -> &'static Value {
    static RUN_ERRORS: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    RUN_ERRORS.get_or_init(|| {
        serde_json::from_str(RUN_ERRORS_TEXT).expect("embedded run errors are JSON")
    })
}

/// Whether run error text is printed as the service sent it (the
/// `debugEnv` variable set to `1`); set once per invocation.
static RAW_RUN_ERRORS: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Reads the run error debug variable from the invocation's environment.
pub fn configure_run_errors(environment: &std::collections::BTreeMap<String, String>) {
    let name = run_errors()["debugEnv"].as_str().unwrap_or_default();
    RAW_RUN_ERRORS.store(
        environment.get(name).is_some_and(|value| value == "1"),
        std::sync::atomic::Ordering::Relaxed,
    );
}

/// The words printed for a hosted run's error text: the first mapped
/// message whose `contains` the text includes, else the generic fallback, so
/// no internal stage name is echoed. The debug variable keeps the text
/// (identical in both ports).
pub fn run_error_text(raw: &str) -> String {
    if RAW_RUN_ERRORS.load(std::sync::atomic::Ordering::Relaxed) {
        return raw.to_owned();
    }
    let table = run_errors();
    table["messages"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|entry| {
            entry["contains"]
                .as_str()
                .is_some_and(|needle| raw.contains(needle))
        })
        .and_then(|entry| entry["message"].as_str())
        .or_else(|| table["fallback"].as_str())
        .unwrap_or_default()
        .to_owned()
}

/// The public billing state of a run (`settled`, `settling` or `unknown`):
/// a charge the service has not settled yet (its `parked`, `pending`) is
/// `settling` (identical in both ports).
pub fn public_billing(raw: &str) -> &'static str {
    match raw {
        "settled" => "settled",
        "settling" | "parked" | "pending" => "settling",
        _ => "unknown",
    }
}

/// The human label of a public billing state: nothing is charged for a run
/// priced at $0.
pub fn billing_label(state: &str, price_cents: Option<i64>) -> String {
    match (state, price_cents) {
        ("", _) => String::new(),
        (_, Some(0)) => "nothing charged".to_owned(),
        ("settling", _) => "settling (the final price is being confirmed)".to_owned(),
        (other, _) => other.to_owned(),
    }
}

/// A run status message in public words: the service's `reactor MODEL`
/// is `Running on MODEL` (identical in both ports).
pub fn status_message(raw: &str) -> String {
    match raw.strip_prefix("reactor") {
        Some("") => "Running".to_owned(),
        Some(rest) if rest.starts_with(' ') => format!("Running on {}", rest.trim_start()),
        _ => raw.to_owned(),
    }
}

/// A service dollar string (`-?D+.DD`, at most 13 whole digits) as whole
/// cents: the `hold_cents` companion of `hold_usd`.
pub fn usd_cents(value: &str) -> Option<i64> {
    let (negative, rest) = value
        .strip_prefix('-')
        .map_or((false, value), |rest| (true, rest));
    let (whole, cents) = rest.split_once('.')?;
    let digits = |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit());
    if !digits(whole) || whole.len() > 13 || cents.len() != 2 || !digits(cents) {
        return None;
    }
    let total = whole.parse::<i64>().ok()? * 100 + cents.parse::<i64>().ok()?;
    Some(if negative { -total } else { total })
}

/// Largest integer both products represent exactly (2^53 - 1).
const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;

/// A JSON number that is a safe integer (`500` or `500.0`), as both products read it.
fn safe_integer(value: &Value) -> Option<i64> {
    if let Some(number) = value.as_i64() {
        return (number.abs() <= MAX_SAFE_INTEGER).then_some(number);
    }
    let float = value.as_f64()?;
    #[allow(clippy::cast_precision_loss)]
    let exact = float.fract() == 0.0 && float.abs() <= MAX_SAFE_INTEGER as f64;
    #[allow(clippy::cast_possible_truncation)]
    exact.then_some(float as i64)
}

/// `plannedRequest.summary`: the non-secret top-level body
/// fields the manifest lists in `confirmation.summaryFields`, so a preview or
/// `CONFIRMATION_REQUIRED` shows what the digest stands for. `None` when the
/// body is not a JSON object or carries none of them.
pub fn body_summary(body: Option<&[u8]>) -> Option<Value> {
    let Value::Object(object) = serde_json::from_slice::<Value>(body?).ok()? else {
        return None;
    };
    let mut summary = Map::new();
    for field in super::manifest()["confirmation"]["summaryFields"].as_array()? {
        let (Some(key), Some(name)) = (field["body"].as_str(), field["field"].as_str()) else {
            continue;
        };
        let Some(value) = object.get(key) else {
            continue;
        };
        let kept = match field["kind"].as_str() {
            Some("text") => value
                .as_str()
                .filter(|text| valid_text(text, 200))
                .map(|text| json!(text)),
            Some("integer" | "cents") => safe_integer(value).map(|number| json!(number)),
            Some("keys") => value.as_object().map(|map| {
                let mut keys = map
                    .keys()
                    .filter(|key| valid_text(key, 200))
                    .collect::<Vec<_>>();
                keys.sort();
                json!(keys)
            }),
            _ => None,
        };
        if let Some(kept) = kept {
            summary.insert(name.to_owned(), kept);
        }
    }
    (!summary.is_empty()).then_some(Value::Object(summary))
}

/// The human label of a `plannedRequest.summary` field (identical in both
/// ports); JSON keeps the field names.
fn summary_label(field: &str) -> &str {
    match field {
        "programRef" => "program",
        "reasoning_effort" => "reasoning effort",
        "inputKeys" => "inputs",
        "interval_seconds" => "every",
        "delivery_mode" => "delivery mode",
        "amount_cents" => "amount",
        other => other,
    }
}

/// A whole number of seconds as a short duration (`24h`, `90m`, `45s`).
pub fn duration_text(seconds: i64) -> String {
    if seconds > 0 && seconds % 3600 == 0 {
        format!("{}h", seconds / 3600)
    } else if seconds > 0 && seconds % 60 == 0 {
        format!("{}m", seconds / 60)
    } else {
        format!("{seconds}s")
    }
}

/// One human line for `plannedRequest.summary`: `label value` pairs in
/// manifest order, input names joined with commas, cents in dollars and an
/// interval as a duration.
pub fn summary_line(summary: &Value) -> Option<String> {
    let object = summary.as_object()?;
    let mut parts = Vec::new();
    for field in super::manifest()["confirmation"]["summaryFields"].as_array()? {
        let Some(name) = field["field"].as_str() else {
            continue;
        };
        let Some(value) = object.get(name) else {
            continue;
        };
        let text = match value {
            Value::Array(items) => items
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", "),
            Value::Number(number) if field["kind"] == "cents" => {
                let cents = number.as_i64().unwrap_or_default();
                let sign = if cents < 0 { "-" } else { "" };
                format!(
                    "${sign}{}.{:02}",
                    cents.unsigned_abs() / 100,
                    cents.unsigned_abs() % 100
                )
            }
            Value::Number(number) if name == "interval_seconds" => {
                duration_text(number.as_i64().unwrap_or_default())
            }
            other => scalar(other),
        };
        parts.push(format!("{} {text}", summary_label(name)));
    }
    (!parts.is_empty()).then(|| human_safe_scalar(&parts.join("; ")))
}

/// What a planned request's effect means for the person, in words
/// (identical in both ports); JSON keeps the effect class.
fn effect_text(planned: &Value) -> String {
    let operation = planned["operation"].as_str().unwrap_or_default();
    let text = match (planned["effect"].as_str().unwrap_or_default(), operation) {
        ("money", "run.submit" | "program.draft") => "starts a paid run",
        ("money", "wallet.topup") => "starts a card payment",
        ("money", "wallet.redeem") => "adds credit to your wallet",
        ("money", id) if id.starts_with("job.") => "starts paid runs",
        ("money", _) => "costs money",
        ("outward", "run.share") => "creates a public 24-hour link that cannot be revoked",
        ("outward", "program.visibility") => "changes who can see the program",
        ("outward", "result.publish") => "publishes a public result",
        ("outward", _) => "makes something public",
        ("destructive", _) => "cannot be undone",
        ("write", "org.create") => {
            "creates an organization; its slug is permanent, so this cannot be undone"
        }
        ("write", "run.cancel") => "stops the run; this cannot be undone",
        ("write", "job.rotate-secret") => {
            "replaces the signing secret; the old one stops working and this cannot be undone"
        }
        ("write", "run.input") => "sends an instruction to the run",
        ("write", "program.save") => "saves a new revision of the program",
        ("write", "result.unpublish") => "removes the published result",
        ("write", "job.create") => "creates a job that starts no runs",
        ("write", "job.update" | "job.configure") => "changes the job",
        ("write", "job.contract.attach") => "attaches a program to the job",
        ("write", "job.contract.detach") => "detaches a program from the job",
        ("write", "org.rename") => "renames the organization",
        ("write", "org.default") => "changes your default organization",
        ("write", "org.member.role") => "changes the member's role",
        ("write", "org.invite") => "creates an invitation to the organization",
        ("write", "org.invitation.accept") => "joins you to the organization",
        ("write", _) => "changes your account",
        ("read", _) => "reads only",
        (other, _) => other,
    };
    human_safe_scalar(text)
}

/// The human line of a schedule job's plan: when its first run fires.
fn first_run_line(planned: &Value) -> Option<String> {
    let interval = planned["summary"]["interval_seconds"].as_i64()?;
    (planned["operation"] == "job.create").then(|| {
        format!(
            "First run: about 1 second after the job is created, then every {}",
            duration_text(interval)
        )
    })
}

/// The human hold line of a plan with a quote (The hold is
/// flat, not an estimate of this run's price).
fn hold_line(planned: &Value) -> Option<String> {
    let hold = planned["quote"]["hold"]["hold_usd"].as_str()?;
    Some(format!(
        "Hold: ${} (set aside from the wallet while the run is live; not its price)",
        human_safe_scalar(hold)
    ))
}

/// A service command's `INVOCATION_INVALID` says what was wrong in plain
/// words instead of the runner's generic message: an unknown command, an
/// unknown option, or another mistake in the command line (identical in both
/// ports). Other errors are unchanged.
pub fn plain_invocation(mut error: RunnerError) -> RunnerError {
    if error.code != ErrorCode::InvocationInvalid
        || error.message != RunnerError::catalog(ErrorCode::InvocationInvalid).message
    {
        return error;
    }
    let reason = error
        .details
        .as_deref()
        .and_then(|details| details.get("reason"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    error.message = if reason.starts_with("unknown command") || reason.contains("is not a command")
    {
        "Unknown command."
    } else if reason.starts_with("unknown option") {
        "Unknown option."
    } else {
        "That command isn't quite right."
    }
    .to_owned();
    error
}

/// The `openprose.service-operation/1` envelope. A paged operation's
/// successful result carries `nextBefore` (null on the last page); an error
/// envelope has none.
pub fn envelope(
    operation: &Value,
    result: &Result<Value, RunnerError>,
    next_before: Option<&str>,
) -> Value {
    let mut value = Map::new();
    value.insert("schema".into(), json!("openprose.service-operation/1"));
    value.insert("operation".into(), operation["id"].clone());
    value.insert("interaction".into(), operation["interaction"].clone());
    match result {
        Ok(result) => {
            let mut result = result.clone();
            if operation["output"]["paged"] == true {
                if let Value::Object(object) = &mut result {
                    object.insert("nextBefore".into(), json!(next_before));
                }
            }
            value.insert("result".into(), result);
            value.insert("problem".into(), Value::Null);
        }
        Err(error) => {
            value.insert("result".into(), Value::Null);
            value.insert(
                "problem".into(),
                serde_json::to_value(plain_invocation(error.clone())).expect("errors serialize"),
            );
        }
    }
    Value::Object(value)
}

/// One `openprose.service-event/1` line.
pub fn event_line(
    run_id: Option<&str>,
    sequence: Option<u64>,
    at: Option<&str>,
    kind: &str,
    data: Value,
) -> Value {
    let mut value = Map::new();
    value.insert("schema".into(), json!("openprose.service-event/1"));
    value.insert("runId".into(), json!(run_id));
    value.insert("sequence".into(), json!(sequence));
    value.insert("at".into(), json!(at));
    value.insert("type".into(), json!(kind));
    value.insert("data".into(), data);
    Value::Object(value)
}

/// An invocation the service surface rejected before an operation ran
///. In JSON and JSONL modes it is always an
/// `openprose.service-operation/1` envelope with `result: null` and the
/// error as `problem`, never a bare runner error: `operation` is the
/// operation the argv names (`None`: the literal `cli`). `environment` is
/// `None` only when the service could not be resolved (an invalid endpoint
/// override in a `dev-endpoint` build); human errors then say `OpenProse`.
/// With both known it is exactly [`outcome`]'s rendering of the error.
pub fn rejected(
    operation: Option<&Value>,
    environment: Option<&Environment>,
    mode: OutputMode,
    error: RunnerError,
) -> CommandOutcome {
    if let (Some(operation), Some(environment)) = (operation, environment) {
        return outcome(operation, environment, mode, Err(error), None);
    }
    let exit = error.exit_code;
    if mode == OutputMode::Human {
        let label = environment.map_or_else(|| "OpenProse".to_owned(), Environment::label);
        return CommandOutcome::human("", human_error(&label, &error), exit);
    }
    let mut value = Map::new();
    value.insert("schema".into(), json!("openprose.service-operation/1"));
    value.insert(
        "operation".into(),
        operation.map_or_else(|| json!("cli"), |operation| operation["id"].clone()),
    );
    value.insert(
        "interaction".into(),
        operation.map_or(Value::Null, |operation| operation["interaction"].clone()),
    );
    value.insert("result".into(), Value::Null);
    value.insert(
        "problem".into(),
        serde_json::to_value(plain_invocation(error)).expect("errors serialize"),
    );
    let document = Value::Object(value);
    if mode == OutputMode::Jsonl {
        CommandOutcome::jsonl(vec![document], exit)
    } else {
        CommandOutcome::json(document, exit)
    }
}

/// Renders the final result or error of an operation.
pub fn outcome(
    operation: &Value,
    environment: &Environment,
    mode: OutputMode,
    result: Result<Value, RunnerError>,
    extras: Option<Extras>,
) -> CommandOutcome {
    let extras = extras.unwrap_or_default();
    let exit = result.as_ref().map_or_else(|error| error.exit_code, |_| 0);
    let document = envelope(operation, &result, extras.next_before.as_deref());
    match mode {
        OutputMode::Json => CommandOutcome::json(document, exit),
        OutputMode::Jsonl if operation["output"]["stream"] == true => {
            // Decision 5: an exit-21 end (the run continues: detached or
            // the --wait deadline) is `service.detached`, so a filter on
            // `*.failed` never mistakes a still-running run for a failure.
            let kind = match &result {
                Ok(_) => "service.completed",
                Err(error) if error.exit_code == 21 => "service.detached",
                Err(_) => "service.failed",
            };
            // The terminal event carries the process exit code.
            let mut line = event_line(extras.run_id.as_deref(), None, None, kind, document);
            line["exitCode"] = json!(exit);
            CommandOutcome::jsonl(vec![line], exit)
        }
        OutputMode::Jsonl => match (&result, operation["output"]["records"].as_str()) {
            (Ok(Value::Object(object)), Some(collection))
                if object.get(collection).is_some_and(Value::is_array) =>
            {
                CommandOutcome::jsonl(
                    record_lines(operation, collection, object, extras.next_before.as_deref()),
                    exit,
                )
            }
            _ => CommandOutcome::jsonl(vec![document], exit),
        },
        OutputMode::Human => match result {
            Ok(result) => {
                let mut text = extras.human.unwrap_or_else(|| human_result(&result));
                if let Some(cursor) = extras.next_before {
                    let mut words = extras
                        .page_words
                        .iter()
                        .map(String::as_str)
                        .collect::<Vec<_>>();
                    words.extend(["--before", cursor.as_str()]);
                    let _ = writeln!(
                        text,
                        "Next page: {}",
                        human_safe_scalar(&argv_text(&follow_up_argv(OutputMode::Human, &words)))
                    );
                }
                CommandOutcome::human(text, "", 0)
            }
            Err(error) => {
                CommandOutcome::human("", human_error(&environment.label(), &error), exit)
            }
        },
    }
}

/// A list operation's JSONL output (decision 5): one
/// `openprose.service-record/1` line per item of the manifest's
/// `output.records` collection, then one `openprose.service-page/1` trailer
/// with the item count, `nextBefore` (null when there is no next page) and
/// `meta`, the rest of the result.
fn record_lines(
    operation: &Value,
    collection: &str,
    result: &Map<String, Value>,
    next_before: Option<&str>,
) -> Vec<Value> {
    let head = |schema: &str| {
        let mut value = Map::new();
        value.insert("schema".into(), json!(schema));
        value.insert("operation".into(), operation["id"].clone());
        value.insert("collection".into(), json!(collection));
        value
    };
    let items = result[collection].as_array().map_or(&[][..], Vec::as_slice);
    let mut lines = items
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let mut line = head("openprose.service-record/1");
            line.insert("index".into(), json!(index));
            line.insert("record".into(), item.clone());
            Value::Object(line)
        })
        .collect::<Vec<_>>();
    let mut page = head("openprose.service-page/1");
    page.insert("interaction".into(), operation["interaction"].clone());
    page.insert("count".into(), json!(items.len()));
    page.insert("nextBefore".into(), json!(next_before));
    let meta = result
        .iter()
        .filter(|(key, _)| key.as_str() != collection)
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<Map<_, _>>();
    page.insert("meta".into(), Value::Object(meta));
    // The trailer ends a successful listing: exitCode is the process exit, 0.
    page.insert("exitCode".into(), json!(0));
    lines.push(Value::Object(page));
    lines
}

/// 9999-12-31T23:59:59.999Z in epoch milliseconds, the last time an RFC 3339
/// year can carry.
pub const MAX_ISO_MS: i64 = 253_402_300_799_999;

/// Epoch milliseconds as an RFC 3339 UTC time with milliseconds, exactly as
/// JavaScript's `toISOString`; `None` for anything else.
pub fn iso_ms(value: &Value) -> Option<String> {
    let ms = value.as_i64().filter(|ms| (0..=MAX_ISO_MS).contains(ms))?;
    chrono::DateTime::from_timestamp_millis(ms)
        .map(|time| time.to_rfc3339_opts(chrono::SecondsFormat::Millis, true))
}

/// The additive `<name>_iso` companions: for each epoch-ms
/// field present in `object`, `<name>_iso` is its RFC 3339 UTC time, or null
/// when the field is null.
pub fn add_iso(object: &mut Map<String, Value>, names: &[&str]) {
    for name in names {
        if let Some(value) = object.get(*name) {
            let iso = iso_ms(value).map_or(Value::Null, Value::String);
            object.insert(format!("{name}_iso"), iso);
        }
    }
}

/// Whole cents as dollars for human output: `$1.23`, `-$0.02` (formatting
/// only; the CLI never computes a price).
pub fn dollars(cents: i64) -> String {
    let sign = if cents < 0 { "-" } else { "" };
    format!(
        "{sign}${}.{:02}",
        cents.unsigned_abs() / 100,
        cents.unsigned_abs() % 100
    )
}

/// A cents value as dollars, or `-` when it is not an integer.
pub fn dollars_or_dash(value: &Value) -> String {
    value.as_i64().map_or_else(|| "-".into(), dollars)
}

/// One human `Next: prose ...` line: a copyable follow-up that
/// comes from the one follow-up renderer. `words` follow `cli`.
pub fn next_line(words: &[&str]) -> String {
    format!(
        "Next: {}\n",
        human_safe_scalar(&argv_text(&follow_up_argv(OutputMode::Human, words)))
    )
}

/// The absolute URL of a service path (`/webhooks/...`) in this environment.
pub fn absolute_url(environment: &Environment, path: &str) -> Option<String> {
    (path.starts_with('/') && !path.starts_with("//"))
        .then(|| format!("{}{path}", environment.origin))
}

fn scalar(value: &Value) -> String {
    match value {
        Value::String(text) => human_safe_scalar(text),
        Value::Null => "null".into(),
        Value::Bool(_) | Value::Number(_) => canonical(value),
        other => human_safe_scalar(&canonical(other)),
    }
}

/// Compact JSON with sorted keys (identical in both products).
pub fn canonical(value: &Value) -> String {
    crate::output::canonical_json(value)
}

/// What a planned request does, in words (its `description`).
fn plan_line(planned: &Value) -> String {
    human_safe_scalar(planned["description"].as_str().unwrap_or_default())
}

/// `Owner: HANDLE (verified as you)` for a plan whose `OWNER/SLUG` was
/// confirmed as the caller.
fn owner_line(planned: &Value) -> Option<String> {
    let handle = planned["owner"]["handle"].as_str()?;
    (planned["owner"]["verified"] == true)
        .then(|| format!("Owner: {} (verified as you)", human_safe_scalar(handle)))
}

/// Default human rendering: one `key: value` line per result field, keys
/// sorted; nested values as canonical JSON.
pub fn human_result(result: &Value) -> String {
    if result["preview"] == true {
        let planned = &result["plannedRequest"];
        let mut text = format!("Preview: {}\n", plan_line(planned));
        if let Some(line) = summary_line(&planned["summary"]) {
            let _ = writeln!(text, "Summary: {line}");
        }
        if let Some(line) = owner_line(planned) {
            let _ = writeln!(text, "{line}");
        }
        let _ = writeln!(text, "Effect: {}", effect_text(planned));
        if let Some(line) = hold_line(planned) {
            let _ = writeln!(text, "{line}");
        }
        if let Some(line) = first_run_line(planned) {
            let _ = writeln!(text, "{line}");
        }
        text.push_str("Nothing was changed.\n");
        return text;
    }
    let Some(object) = result.as_object() else {
        return format!("{}\n", scalar(result));
    };
    let mut keys = object.keys().collect::<Vec<_>>();
    keys.sort();
    keys.into_iter().fold(String::new(), |mut text, key| {
        let _ = writeln!(text, "{}: {}", human_safe_scalar(key), scalar(&object[key]));
        text
    })
}

/// Shell-quotes one argument for a copyable command line.
pub fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_./:=@%+,-".contains(&byte))
    {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

/// The one follow-up argv renderer. Every copyable command the
/// CLI prints or returns (`resumeArgv`, `cancelArgv`, `suggestedArgv`, and the
/// commands inside reasons and actions) keeps the invocation's machine output
/// mode, so a copied command never silently switches to human output. The
/// service is fixed by the build, so no command names one. `words` follow
/// `cli`.
pub fn follow_up_argv(mode: OutputMode, words: &[&str]) -> Vec<String> {
    let mut argv = Vec::new();
    match mode {
        OutputMode::Human => {}
        OutputMode::Json => argv.extend(["--output".to_owned(), "json".to_owned()]),
        OutputMode::Jsonl => argv.extend(["--output".to_owned(), "jsonl".to_owned()]),
    }
    argv.push("cli".to_owned());
    argv.extend(words.iter().map(|word| (*word).to_owned()));
    argv
}

/// The program that starts every copyable command: `prose` in a public
/// build. A `dev-endpoint` build names the executable it was invoked as
/// (see [`record_invoked_name`]), so a copied command re-runs the same
/// build against the same endpoint instead of the public `prose`.
pub fn product() -> &'static str {
    #[cfg(feature = "dev-endpoint")]
    if let Some(name) = super::dev_endpoint::invoked_name() {
        return name;
    }
    "prose"
}

/// Records `argv[0]` as the program copyable commands name. Only a
/// `dev-endpoint` build honors it; a public build always prints `prose`.
pub fn record_invoked_name(argv0: Option<&str>) {
    #[cfg(feature = "dev-endpoint")]
    super::dev_endpoint::record_invoked_name(argv0);
    #[cfg(not(feature = "dev-endpoint"))]
    let _ = argv0;
}

/// Rewrites every `` `prose ...` `` command in a fixed text to name
/// [`product`]. The identity in a public build.
pub fn localize_product(text: &str) -> String {
    let product = product();
    if product == "prose" {
        return text.to_owned();
    }
    text.replace("`prose ", &format!("`{product} "))
}

/// A help topic as this build prints it: `help.v1.json` stores `prose` as
/// the program placeholder, and every command in it (a line starting with
/// `prose `, the `Usage: prose` line and every `` `prose ...` `` span) names
/// [`product`] instead, the same program errors name. The identity in a
/// public build.
pub fn localize_help(text: &str) -> String {
    localize_help_for(product(), text)
}

/// [`localize_help`] with an explicit program.
pub fn localize_help_for(product: &str, text: &str) -> String {
    if product == "prose" {
        return text.to_owned();
    }
    text.split_inclusive('\n')
        .map(|line| {
            let indent = line.len() - line.trim_start_matches(' ').len();
            let (head, rest) = line.split_at(indent);
            let rest = if let Some(tail) = rest.strip_prefix("prose ") {
                format!("{product} {tail}")
            } else if let Some(tail) = rest.strip_prefix("Usage: prose ") {
                format!("Usage: {product} {tail}")
            } else {
                rest.to_owned()
            };
            format!("{head}{}", rest.replace("`prose ", &format!("`{product} ")))
        })
        .collect()
}

/// A copyable command line (`prose ...`, shell-quoted, not yet made
/// terminal-safe) for an argv after the product name.
pub fn argv_text(argv: &[String]) -> String {
    argv_text_for(product(), argv)
}

/// [`argv_text`] with an explicit program (already shell-quoted).
pub fn argv_text_for(product: &str, argv: &[String]) -> String {
    let words = argv
        .iter()
        .map(|word| shell_quote(word))
        .collect::<Vec<_>>();
    format!("{product} {}", words.join(" "))
}

/// A copyable follow-up command line; `words` (space-separated) follow `cli`.
pub fn follow_up_command(mode: OutputMode, words: &str) -> String {
    argv_text(&follow_up_argv(
        mode,
        &words
            .split(' ')
            .filter(|word| !word.is_empty())
            .collect::<Vec<_>>(),
    ))
}

fn argv_line(argv: &Value) -> Option<String> {
    let words = argv
        .as_array()?
        .iter()
        .map(|word| word.as_str().map(shell_quote))
        .collect::<Option<Vec<_>>>()?;
    Some(human_safe_scalar(&format!(
        "{} {}",
        product(),
        words.join(" ")
    )))
}

/// Human error text (stderr). Identical in both products.
pub fn human_error(label: &str, error: &RunnerError) -> String {
    let error = &plain_invocation(error.clone());
    let empty = Map::new();
    let details = error.details.as_deref().unwrap_or(&empty);
    let mut text = format!(
        "{label}: {}: {}\n",
        error.code,
        human_safe_scalar(&error.message)
    );
    if let Some(reason) = details.get("reason").and_then(Value::as_str) {
        let _ = writeln!(text, "Detail: {}", crate::error::human_safe_detail(reason));
    }
    // The HTTP status stays in JSON (`details.serviceStatus`); a person reads
    // the cause in the headline and Detail, and the service's own code.
    if let Some(code) = details.get("serviceCode").and_then(Value::as_str) {
        let _ = writeln!(text, "Service code: {}", human_safe_scalar(code));
    }
    if let Some(message) = details.get("serviceMessage").and_then(Value::as_str) {
        let _ = writeln!(text, "Service message: {}", human_safe_scalar(message));
    }
    if let Some(url) = details.get("webUrl").and_then(Value::as_str) {
        let _ = writeln!(text, "Web: {}", human_safe_scalar(url));
    }
    if let Some(run) = details.get("runId").and_then(Value::as_str) {
        let after = details
            .get("afterSequence")
            .map(|after| format!(" (after sequence {after})"))
            .unwrap_or_default();
        let _ = writeln!(text, "Run: {}{after}", human_safe_scalar(run));
    }
    if let Some(planned) = details.get("plannedRequest") {
        let _ = writeln!(text, "Planned: {}", plan_line(planned));
        if let Some(line) = summary_line(&planned["summary"]) {
            let _ = writeln!(text, "Planned summary: {line}");
        }
        if let Some(line) = owner_line(planned) {
            let _ = writeln!(text, "{line}");
        }
        if let Some(line) = hold_line(planned) {
            let _ = writeln!(text, "{line}");
        }
        if let Some(line) = first_run_line(planned) {
            let _ = writeln!(text, "{line}");
        }
    }
    if let Some(line) = details.get("confirmArgv").and_then(argv_line) {
        let _ = writeln!(text, "Confirm with: {line}");
    }
    if let Some(line) = details.get("previewArgv").and_then(argv_line) {
        let _ = writeln!(text, "Preview with: {line}");
    }
    let _ = writeln!(
        text,
        "Action: {}",
        human_safe_scalar(&human_action(error, details))
    );
    text
}

/// The Action as a person reads it: JSON keeps the catalog Action, which
/// names `details.*` fields; human output names the lines printed above
/// instead (identical in both ports). An Action a handler replaced is kept.
fn human_action(error: &RunnerError, details: &Map<String, Value>) -> String {
    let has = |key: &str| details.get(key).is_some_and(Value::is_string);
    if error.code == ErrorCode::ServiceRequestRejected
        && error.action.starts_with("Correct the request as details.")
    {
        let present = [
            ("reason", "Detail"),
            ("serviceMessage", "Service message"),
            ("serviceCode", "Service code"),
        ]
        .into_iter()
        .filter(|(key, _)| has(key))
        .map(|(_, label)| label)
        .collect::<Vec<_>>();
        if let [rest @ .., last] = present.as_slice() {
            return if rest.is_empty() {
                format!("Correct the request as the {last} line above describes, then retry.")
            } else {
                format!(
                    "Correct the request as the {} and {last} lines above describe, then retry.",
                    rest.join(", ")
                )
            };
        }
    }
    if !has("reason") || error.action != RunnerError::catalog(error.code).action {
        return error.action.clone();
    }
    let text = match error.code {
        ErrorCode::ServiceWatchDeadline => {
            "The run continues; keep following it with the command in Detail above."
        }
        ErrorCode::HostedRunDetached => {
            "Keep following the run with the command in Detail above; never submit the run again to resume it, because that starts a second paid run."
        }
        ErrorCode::HostedRunCancelled => {
            "Read the cancelled run with the command in Detail above; submit again only to start a new run."
        }
        ErrorCode::RunSubmissionAmbiguous => {
            "Check recent runs with the command in Detail above before submitting again; submitting again could start a second paid run."
        }
        ErrorCode::ExampleNotViewable => {
            "Read the example at the Web address above when one is printed, or pick an example that `cli example list` does not mark for viewing on the web."
        }
        _ => return error.action.clone(),
    };
    text.to_owned()
}

/// A small regular-expression matcher for fixture header assertions
/// (`requestHeaders.<name>.pattern`). Supports literals, `.`, classes with
/// ranges and negation, `\d \w \s` and escaped literals, groups with `|`,
/// `(?:…)`, the quantifiers `* + ? {n} {n,} {n,m}` and the anchors `^ $`.
/// Returns `None` for syntax outside that subset.
pub fn pattern_matches(pattern: &str, value: &str) -> Option<bool> {
    let chars = pattern.chars().collect::<Vec<_>>();
    let mut parser = Parser {
        chars: &chars,
        index: 0,
    };
    let tree = parser.alternation()?;
    if parser.index != chars.len() {
        return None;
    }
    let input = value.chars().collect::<Vec<_>>();
    let nodes = [tree];
    Some((0..=input.len()).any(|start| sequence(&nodes, start, &input, &|_| true)))
}

#[derive(Debug, Clone)]
enum Node {
    Literal(char),
    Any,
    Class(Vec<ClassItem>, bool),
    Start,
    End,
    Alternation(Vec<Vec<Node>>),
    Repeat(Box<Node>, usize, usize),
}

#[derive(Debug, Clone)]
enum ClassItem {
    Range(char, char),
    Digit,
    Word,
    Space,
}

impl ClassItem {
    fn matches(&self, character: char) -> bool {
        match self {
            Self::Range(low, high) => (*low..=*high).contains(&character),
            Self::Digit => character.is_ascii_digit(),
            Self::Word => character.is_ascii_alphanumeric() || character == '_',
            Self::Space => character.is_whitespace(),
        }
    }
}

struct Parser<'a> {
    chars: &'a [char],
    index: usize,
}

impl Parser<'_> {
    fn peek(&self) -> Option<char> {
        self.chars.get(self.index).copied()
    }

    fn alternation(&mut self) -> Option<Node> {
        let mut branches = vec![self.sequence()?];
        while self.peek() == Some('|') {
            self.index += 1;
            branches.push(self.sequence()?);
        }
        Some(Node::Alternation(branches))
    }

    fn sequence(&mut self) -> Option<Vec<Node>> {
        let mut nodes = Vec::new();
        while let Some(character) = self.peek() {
            if matches!(character, '|' | ')') {
                break;
            }
            let atom = self.atom()?;
            nodes.push(self.quantifier(atom)?);
        }
        Some(nodes)
    }

    fn quantifier(&mut self, atom: Node) -> Option<Node> {
        let (min, max) = match self.peek() {
            Some('*') => (0, usize::MAX),
            Some('+') => (1, usize::MAX),
            Some('?') => (0, 1),
            Some('{') => {
                let close = self.chars[self.index..].iter().position(|c| *c == '}')? + self.index;
                let body = self.chars[self.index + 1..close].iter().collect::<String>();
                let (min, max) = match body.split_once(',') {
                    Some((min, "")) => (min.parse().ok()?, usize::MAX),
                    Some((min, max)) => (min.parse().ok()?, max.parse().ok()?),
                    None => {
                        let count = body.parse().ok()?;
                        (count, count)
                    }
                };
                self.index = close;
                (min, max)
            }
            _ => return Some(atom),
        };
        if matches!(atom, Node::Start | Node::End) || min > max {
            return None;
        }
        self.index += 1;
        Some(Node::Repeat(Box::new(atom), min, max))
    }

    fn escape(&mut self) -> Option<ClassItem> {
        let character = self.peek()?;
        self.index += 1;
        Some(match character {
            'd' => ClassItem::Digit,
            'w' => ClassItem::Word,
            's' => ClassItem::Space,
            'n' => ClassItem::Range('\n', '\n'),
            't' => ClassItem::Range('\t', '\t'),
            other if !other.is_ascii_alphanumeric() => ClassItem::Range(other, other),
            _ => return None,
        })
    }

    fn atom(&mut self) -> Option<Node> {
        let character = self.peek()?;
        self.index += 1;
        Some(match character {
            '^' => Node::Start,
            '$' => Node::End,
            '.' => Node::Any,
            '(' => {
                if self.chars[self.index..].starts_with(&['?', ':']) {
                    self.index += 2;
                } else if self.peek() == Some('?') {
                    return None;
                }
                let inner = self.alternation()?;
                if self.peek() != Some(')') {
                    return None;
                }
                self.index += 1;
                inner
            }
            '[' => {
                let negated = self.peek() == Some('^');
                if negated {
                    self.index += 1;
                }
                let mut items = Vec::new();
                loop {
                    let current = self.peek()?;
                    self.index += 1;
                    if current == ']' && !items.is_empty() {
                        break;
                    }
                    let item = if current == '\\' {
                        self.escape()?
                    } else {
                        ClassItem::Range(current, current)
                    };
                    if let (ClassItem::Range(low, _), Some('-')) = (&item, self.peek()) {
                        if self
                            .chars
                            .get(self.index + 1)
                            .is_some_and(|next| *next != ']')
                        {
                            let low = *low;
                            self.index += 1;
                            let high = self.peek()?;
                            self.index += 1;
                            items.push(ClassItem::Range(low, high));
                            continue;
                        }
                    }
                    items.push(item);
                }
                Node::Class(items, negated)
            }
            '\\' => match self.escape()? {
                ClassItem::Range(low, _) => Node::Literal(low),
                item => Node::Class(vec![item], false),
            },
            '*' | '+' | '?' | '{' | ')' => return None,
            literal => Node::Literal(literal),
        })
    }
}

fn single(node: &Node, character: char) -> bool {
    match node {
        Node::Literal(expected) => *expected == character,
        Node::Any => character != '\n',
        Node::Class(items, negated) => items.iter().any(|item| item.matches(character)) != *negated,
        _ => false,
    }
}

fn sequence(nodes: &[Node], position: usize, input: &[char], next: &dyn Fn(usize) -> bool) -> bool {
    let Some((first, rest)) = nodes.split_first() else {
        return next(position);
    };
    let continuation = |after: usize| sequence(rest, after, input, next);
    one(first, position, input, &continuation)
}

fn one(node: &Node, position: usize, input: &[char], next: &dyn Fn(usize) -> bool) -> bool {
    match node {
        Node::Start => position == 0 && next(position),
        Node::End => position == input.len() && next(position),
        Node::Alternation(branches) => branches
            .iter()
            .any(|branch| sequence(branch, position, input, next)),
        Node::Repeat(inner, min, max) => repeat(inner, *min, *max, 0, position, input, next),
        atom => {
            input
                .get(position)
                .is_some_and(|character| single(atom, *character))
                && next(position + 1)
        }
    }
}

fn repeat(
    node: &Node,
    min: usize,
    max: usize,
    count: usize,
    position: usize,
    input: &[char],
    next: &dyn Fn(usize) -> bool,
) -> bool {
    if count < max
        && one(node, position, input, &|after| {
            after != position && repeat(node, min, max, count + 1, after, input, next)
        })
    {
        return true;
    }
    count >= min && next(position)
}

#[cfg(test)]
mod tests {
    #[test]
    fn localize_help_matches_the_shared_fixture() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/human/help-program-name.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            assert_eq!(
                localize_help_for(
                    case["program"].as_str().unwrap(),
                    case["input"].as_str().unwrap()
                ),
                case["rendered"].as_str().unwrap(),
                "{}",
                case["id"]
            );
        }
    }

    use super::*;

    #[test]
    fn patterns_cover_client_identity_headers() {
        let pattern = r"^cli/[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\+(rust|bun)$";
        assert_eq!(pattern_matches(pattern, "cli/0.1.0+rust"), Some(true));
        assert_eq!(pattern_matches(pattern, "cli/0.1.0+bun"), Some(true));
        assert_eq!(pattern_matches(pattern, "cli/0.1.0+go"), Some(false));
        assert_eq!(
            pattern_matches(
                "^Bearer rr_test_[0-9a-f]{32}$",
                &format!("Bearer rr_test_{}", "a".repeat(32))
            ),
            Some(true)
        );
        assert_eq!(pattern_matches("prose", "x prose-cli/1"), Some(true));
        assert_eq!(pattern_matches("(?=x)", "x"), None);
        assert_eq!(pattern_matches("[^a-c]+$", "xyz"), Some(true));
        assert_eq!(pattern_matches("^a{2,3}$", "aaaa"), Some(false));
    }

    #[test]
    fn redaction_and_messages() {
        assert_eq!(redact("key rr_test_abc123 end", None), "key [REDACTED] end");
        assert_eq!(sanitize_service_message("\u{7}\n", None), None);
        assert_eq!(
            sanitize_service_message(&"é".repeat(600), None)
                .unwrap()
                .chars()
                .count(),
            512
        );
    }

    #[test]
    fn shell_quoting_is_copyable() {
        assert_eq!(shell_quote("run_1"), "run_1");
        assert_eq!(shell_quote("it's"), "'it'\\''s'");
        assert_eq!(shell_quote(""), "''");
    }
}
