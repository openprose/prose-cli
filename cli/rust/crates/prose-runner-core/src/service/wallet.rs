//! Service wallet operations: `wallet balance|events|usage|redeem|topup`.
//!
//! Every projection is closed and carries only server-provided price fields
//! (`*_cents`, `*_dollars`, `*_usd`); the CLI never computes a price. The
//! redeem code is read from a file or standard input and never echoed; the
//! top-up always sends an `Idempotency-Key` (a minted `UUIDv4` unless
//! `--idempotency-key` is given) and prints the Checkout URL without opening a
//! browser. Mirrors `cli/bun/src/core/service/wallet.ts`.
use super::render::{dollars_or_dash, sanitize_service_message, valid_text};
use super::{Context, Gate, http};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
use std::fmt::Write as _;

/// Default and maximum `wallet events --limit`.
const EVENTS_DEFAULT_LIMIT: u64 = 20;
const EVENTS_MAX_LIMIT: u64 = 100;
/// The redeem route accepts at most 1 KiB of body; a code file is far smaller.
const CODE_FILE_MAX_BYTES: u64 = 1024;
const CODE_MAX_CHARS: usize = 64;
/// Largest integer both products represent exactly (2^53 - 1).
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.invocation.operation.as_str() {
        "wallet.balance" => balance(context),
        "wallet.events" => events(context),
        "wallet.usage" => usage(context),
        "wallet.redeem" => redeem(context),
        "wallet.topup" => topup(context),
        _ => context.not_implemented(),
    }
}

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn protocol(reason: &str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
        .with_detail("reason", format!("unexpected wallet response: {reason}"))
}

// ---------------------------------------------------------------- validators

/// A server identifier or label: at most `max` code points, no C0 or DEL
/// (empty allowed, matching the closed result schema).
fn plain_text(value: &Value, max: usize, field: &str) -> Result<Value, RunnerError> {
    match value.as_str() {
        Some(text) if text.is_empty() || valid_text(text, max) => Ok(json!(text)),
        _ => Err(protocol(field)),
    }
}

/// Free service text (event descriptions, notes): control characters become
/// spaces, keys are redacted, the text is cut to 512 code points.
fn free_text(value: &Value, field: &str) -> Result<String, RunnerError> {
    let text = value.as_str().ok_or_else(|| protocol(field))?;
    Ok(sanitize_service_message(text, None).unwrap_or_default())
}

/// An exact integer. A JSON number with a zero fraction (`5.0`) is accepted
/// and emitted as an integer so both products print it identically.
fn integer(value: &Value, field: &str, minimum: Option<i64>) -> Result<Value, RunnerError> {
    let number = if let Some(number) = value.as_i64() {
        Some(number)
    } else {
        value.as_f64().and_then(|float| {
            #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
            let exact = float.fract() == 0.0 && float.abs() <= MAX_SAFE_INTEGER as f64;
            #[allow(clippy::cast_possible_truncation)]
            exact.then_some(float as i64)
        })
    };
    match number {
        #[allow(clippy::cast_possible_wrap)]
        Some(number)
            if number.unsigned_abs() <= MAX_SAFE_INTEGER
                && minimum.is_none_or(|min| number >= min) =>
        {
            Ok(json!(number))
        }
        _ => Err(protocol(field)),
    }
}

fn number(value: &Value, field: &str) -> Result<Value, RunnerError> {
    if value.is_number() {
        Ok(value.clone())
    } else {
        Err(protocol(field))
    }
}

fn dollars(value: &Value, field: &str) -> Result<Value, RunnerError> {
    let text = value.as_str().ok_or_else(|| protocol(field))?;
    let (whole, cents) = text
        .strip_prefix('-')
        .unwrap_or(text)
        .split_once('.')
        .ok_or_else(|| protocol(field))?;
    let digits = |part: &str| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit());
    if digits(whole) && cents.len() == 2 && digits(cents) {
        Ok(json!(text))
    } else {
        Err(protocol(field))
    }
}

fn timestamp(value: &Value, field: &str) -> Result<Value, RunnerError> {
    let text = value.as_str().ok_or_else(|| protocol(field))?;
    let bytes = text.as_bytes();
    let ok = text.len() <= 40
        && bytes.len() >= 12
        && text.get(..10).is_some_and(is_date)
        && bytes[10] == b'T'
        && bytes[bytes.len() - 1] == b'Z'
        && bytes[11..bytes.len() - 1]
            .iter()
            .all(|byte| byte.is_ascii_digit() || *byte == b':' || *byte == b'.');
    if ok {
        Ok(json!(text))
    } else {
        Err(protocol(field))
    }
}

/// `YYYY-MM-DD` naming a real proleptic-Gregorian calendar day (leap years
/// included). The service answers any other date with a 500.
fn is_date(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return false;
    }
    let digits = |range: std::ops::Range<usize>| -> Option<u32> {
        text.get(range)
            .filter(|part| part.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|part| part.parse().ok())
    };
    match (digits(0..4), digits(5..7), digits(8..10)) {
        (Some(year), Some(month), Some(day)) => i32::try_from(year)
            .ok()
            .and_then(|year| chrono::NaiveDate::from_ymd_opt(year, month, day))
            .is_some(),
        _ => false,
    }
}

fn date(value: &Value, field: &str) -> Result<Value, RunnerError> {
    match value.as_str() {
        Some(text) if is_date(text) => Ok(json!(text)),
        _ => Err(protocol(field)),
    }
}

fn object<'v>(value: &'v Value, field: &str) -> Result<&'v Map<String, Value>, RunnerError> {
    value.as_object().ok_or_else(|| protocol(field))
}

static NULL: Value = Value::Null;

/// The field's value, or JSON null when it is missing.
fn absent<'m>(map: &'m Map<String, Value>, key: &str) -> &'m Value {
    map.get(key).unwrap_or(&NULL)
}

// ------------------------------------------------------------------- balance

/// Projects the service balance (drops `customer_id` and `*_nanos`).
pub fn project_balance(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let balance = object(absent(body, "balance"), "balance")?;
    let mut projected = Map::new();
    for field in ["available", "posted", "reserved"] {
        let cents = format!("{field}_cents");
        let dollar = format!("{field}_dollars");
        projected.insert(
            cents.clone(),
            integer(absent(balance, &cents), &format!("balance.{cents}"), None)?,
        );
        projected.insert(
            dollar.clone(),
            dollars(absent(balance, &dollar), &format!("balance.{dollar}"))?,
        );
    }
    Ok(Value::Object(projected))
}

fn balance(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let request = http::Request::from_manifest(context.operation, 0, "/wallet/balance");
    let body = context.send(&request)?.json_object()?;
    let balance = project_balance(&body)?;
    context.human = Some(human_balance(&balance));
    Ok(json!({"balance": balance}))
}

pub fn human_balance(balance: &Value) -> String {
    let mut text = String::new();
    for (label, field) in [
        ("Available", "available"),
        ("Reserved", "reserved"),
        ("Balance", "posted"),
    ] {
        let _ = writeln!(
            text,
            "{label}: ${}",
            human_safe_scalar(
                balance[format!("{field}_dollars")]
                    .as_str()
                    .unwrap_or_default()
            )
        );
    }
    text
}

// -------------------------------------------------------------------- events

/// `--limit`: an integer from 1 to 100 (default 20).
pub fn parse_events_limit(value: Option<&str>) -> Result<u64, RunnerError> {
    let Some(value) = value else {
        return Ok(EVENTS_DEFAULT_LIMIT);
    };
    value
        .parse::<u64>()
        .ok()
        .filter(|limit| {
            (1..=EVENTS_MAX_LIMIT).contains(limit)
                && value.bytes().all(|byte| byte.is_ascii_digit())
        })
        .ok_or_else(|| {
            invalid(format!(
                "--limit must be an integer from 1 to {EVENTS_MAX_LIMIT}, got {value_quoted}",
                value_quoted = crate::error::quote(value)
            ))
        })
}

/// Projects one events page; returns the result and the next cursor.
pub fn project_events(body: &Map<String, Value>) -> Result<(Value, Option<String>), RunnerError> {
    let Some(items) = body.get("events").and_then(Value::as_array) else {
        return Err(protocol("events"));
    };
    if items.len() as u64 > EVENTS_MAX_LIMIT {
        return Err(protocol("events (more than 100)"));
    }
    let mut events = Vec::with_capacity(items.len());
    for item in items {
        let item = object(item, "events[]")?;
        let mut event = Map::new();
        event.insert(
            "id".into(),
            plain_text(absent(item, "id"), 64, "events[].id")?,
        );
        event.insert(
            "type".into(),
            plain_text(absent(item, "type"), 64, "events[].type")?,
        );
        event.insert(
            "occurred_at".into(),
            timestamp(absent(item, "occurred_at"), "events[].occurred_at")?,
        );
        event.insert(
            "description".into(),
            json!(free_text(
                absent(item, "description"),
                "events[].description"
            )?),
        );
        event.insert(
            "amount_cents".into(),
            integer(absent(item, "amount_cents"), "events[].amount_cents", None)?,
        );
        event.insert(
            "balance_after_cents".into(),
            integer(
                absent(item, "balance_after_cents"),
                "events[].balance_after_cents",
                None,
            )?,
        );
        match item.get("ref") {
            None => {}
            Some(Value::Null) => {
                event.insert("ref".into(), Value::Null);
            }
            Some(value) => {
                event.insert("ref".into(), plain_text(value, 256, "events[].ref")?);
            }
        }
        events.push(Value::Object(event));
    }
    let mut result = Map::new();
    result.insert("events".into(), Value::Array(events));
    match body.get("note") {
        None | Some(Value::Null) => {}
        Some(value) => {
            let note = free_text(value, "note")?;
            if !note.trim().is_empty() {
                result.insert("note".into(), json!(note));
            }
        }
    }
    let next = match body.get("next_before") {
        None | Some(Value::Null) => None,
        Some(Value::String(cursor)) if valid_text(cursor, 512) => Some(cursor.clone()),
        Some(_) => return Err(protocol("next_before")),
    };
    Ok((Value::Object(result), next))
}

fn events(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let limit = parse_events_limit(context.option("--limit")).map_err(|error| {
        context.limit_error(
            error,
            context.option("--limit").unwrap_or_default(),
            EVENTS_MAX_LIMIT,
        )
    })?;
    let before = context.option("--before").map(str::to_owned);
    if let Some(cursor) = &before {
        if !valid_text(cursor, 512) {
            return Err(invalid(
                "--before must be a cursor from a previous result's nextBefore (1 to 512 characters, no control characters)",
            ));
        }
    }
    let mut request = http::Request::from_manifest(context.operation, 0, "/wallet/events")
        .query("limit", limit.to_string());
    if let Some(cursor) = before {
        request = request.query("before", cursor);
    }
    let body = context.send(&request)?.json_object()?;
    let (result, next) = project_events(&body)?;
    context.human = Some(human_events(&result));
    context.next_before = next;
    Ok(result)
}

/// The human label of a wallet event (identical in both ports): the type in
/// words, then the service's description without its internal `Contract
/// run` prefix (`run charge: model-luna`). A description that already starts
/// with the type is printed once, alone (`Credit code`, not `credit: Credit
/// code`), and an empty one falls back to the type. JSON keeps both fields
/// as sent.
fn event_label(kind: &str, description: &str) -> String {
    let kind = kind.replace('_', " ");
    let description = description
        .strip_prefix("Contract run")
        .map_or(description, str::trim_start);
    if description.is_empty() {
        kind
    } else if description.to_lowercase().starts_with(&kind.to_lowercase()) {
        description.to_owned()
    } else {
        format!("{kind}: {description}")
    }
}

pub fn human_events(result: &Value) -> String {
    let events = result["events"].as_array().map_or(&[][..], Vec::as_slice);
    let mut text = String::new();
    if events.is_empty() {
        text.push_str("No wallet events.\n");
    }
    for event in events {
        let _ = writeln!(
            text,
            "{}  {}  {}  balance {}  {}",
            human_safe_scalar(event["occurred_at"].as_str().unwrap_or_default()),
            human_safe_scalar(event["id"].as_str().unwrap_or_default()),
            dollars_or_dash(&event["amount_cents"]),
            dollars_or_dash(&event["balance_after_cents"]),
            human_safe_scalar(&event_label(
                event["type"].as_str().unwrap_or_default(),
                event["description"].as_str().unwrap_or_default(),
            )),
        );
    }
    if let Some(note) = result["note"].as_str() {
        let _ = writeln!(text, "Note: {}", human_safe_scalar(note));
    }
    text
}

// --------------------------------------------------------------------- usage

/// The server's default usage window, as `(default_start, default_end)` UTC
/// dates for the instant `now` (RFC 3339): `now - 30 days` and today. The
/// service fills a missing bound this way.
pub fn default_usage_window(now: &str) -> Option<(String, String)> {
    let now = chrono::DateTime::parse_from_rfc3339(now)
        .ok()?
        .with_timezone(&chrono::Utc);
    let start = now - chrono::Duration::days(30);
    Some((
        start.format("%Y-%m-%d").to_string(),
        now.format("%Y-%m-%d").to_string(),
    ))
}

/// A `YYYY-M-D` date with its month and day zero-padded (`2026-9-1` ->
/// `2026-09-01`), when that is a real calendar date.
fn padded_date(value: &str) -> Option<String> {
    let mut parts = value.split('-');
    let (year, month, day) = (parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some()
        || year.len() != 4
        || !(1..=2).contains(&month.len())
        || !(1..=2).contains(&day.len())
        || ![year, month, day]
            .iter()
            .all(|part| part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return None;
    }
    let padded = format!("{year}-{month:0>2}-{day:0>2}");
    is_date(&padded).then_some(padded)
}

/// `argv` with the value of `option` replaced (separate or `=` form).
fn with_option_value(argv: &[String], option: &str, value: &str) -> Vec<String> {
    let prefix = format!("{option}=");
    let mut out = argv.to_vec();
    let mut index = 0;
    while index < out.len() {
        if out[index] == option && index + 1 < out.len() {
            value.clone_into(&mut out[index + 1]);
            index += 1;
        } else if out[index].starts_with(&prefix) {
            out[index] = format!("{prefix}{value}");
        }
        index += 1;
    }
    out
}

/// `--start` / `--end`: real calendar dates as `YYYY-MM-DD`, start not after
/// end. A lone bound is checked against the server-defaulted other bound
/// (derived from `now`, RFC 3339), because an inverted window is a permanent
/// server failure, never a retryable one.
pub fn usage_window(
    start: Option<&str>,
    end: Option<&str>,
    now: &str,
) -> Result<Vec<(String, String)>, RunnerError> {
    let mut query = Vec::new();
    for (name, value) in [("start", start), ("end", end)] {
        if let Some(value) = value {
            if !is_date(value) {
                return Err(invalid(format!(
                    "--{name} must be a calendar date as YYYY-MM-DD, got {value_quoted}",
                    value_quoted = crate::error::quote(value)
                )));
            }
            query.push((name.to_owned(), value.to_owned()));
        }
    }
    if let (Some(start), Some(end)) = (start, end) {
        if start > end {
            return Err(invalid(format!(
                "--start {start} is after --end {end}; swap them"
            )));
        }
    }
    if start.is_some() != end.is_some() {
        if let Some((default_start, default_end)) = default_usage_window(now) {
            if let Some(start) = start.filter(|start| *start > default_end.as_str()) {
                return Err(invalid(format!(
                    "--start {start} is after today ({default_end} UTC), the default --end; pass an earlier --start or an explicit --end"
                )));
            }
            if let Some(end) = end.filter(|end| *end < default_start.as_str()) {
                return Err(invalid(format!(
                    "--end {end} is before the default --start ({default_start} UTC, 30 days ago); pass an explicit --start"
                )));
            }
        }
    }
    Ok(query)
}

pub fn project_usage(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let period = object(absent(body, "period"), "period")?;
    let Some(days) = body.get("daily").and_then(Value::as_array) else {
        return Err(protocol("daily"));
    };
    if days.len() > 400 {
        return Err(protocol("daily (more than 400 days)"));
    }
    let mut daily = Vec::with_capacity(days.len());
    for day in days {
        let day = object(day, "daily[]")?;
        daily.push(json!({
            "date": date(absent(day, "date"), "daily[].date")?,
            "runs": integer(absent(day, "runs"), "daily[].runs", Some(0))?,
            "input_tokens": integer(absent(day, "input_tokens"), "daily[].input_tokens", Some(0))?,
            "output_tokens": integer(absent(day, "output_tokens"), "daily[].output_tokens", Some(0))?,
            "price_cents": integer(absent(day, "price_cents"), "daily[].price_cents", None)?,
        }));
    }
    Ok(json!({
        "period": {
            "start": date(absent(period, "start"), "period.start")?,
            "end": date(absent(period, "end"), "period.end")?,
        },
        "total_runs": integer(absent(body, "total_runs"), "total_runs", Some(0))?,
        "total_input_tokens": integer(absent(body, "total_input_tokens"), "total_input_tokens", Some(0))?,
        "total_output_tokens": integer(absent(body, "total_output_tokens"), "total_output_tokens", Some(0))?,
        "total_price_cents": integer(absent(body, "total_price_cents"), "total_price_cents", None)?,
        "daily": daily,
    }))
}

fn usage(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let now = context.now_rfc3339();
    let start = context.option("--start");
    let end = context.option("--end");
    let query = usage_window(start, end, &now).map_err(|error| {
        // `2026-9-1` -> `2026-09-01`; an inverted window is swapped.
        let padded = |value: Option<&str>| {
            value.map(|value| padded_date(value).unwrap_or_else(|| value.to_owned()))
        };
        let (mut fixed_start, mut fixed_end) = (padded(start), padded(end));
        if let (Some(left), Some(right)) = (&fixed_start, &fixed_end) {
            if left > right && is_date(left) && is_date(right) {
                std::mem::swap(&mut fixed_start, &mut fixed_end);
            }
        }
        let changed = fixed_start.as_deref() != start || fixed_end.as_deref() != end;
        if !changed || usage_window(fixed_start.as_deref(), fixed_end.as_deref(), &now).is_err() {
            return error;
        }
        let mut argv = context.invocation.argv.clone();
        for (option, value) in [("--start", &fixed_start), ("--end", &fixed_end)] {
            if let Some(value) = value {
                if context.option(option).is_some() {
                    argv = with_option_value(&argv, option, value);
                }
            }
        }
        context.corrected(error, "Use the corrected dates: `{command}`", argv)
    })?;
    let mut request = http::Request::from_manifest(context.operation, 0, "/wallet/usage");
    for (name, value) in query {
        request = request.query(&name, value);
    }
    let body = context.send(&request)?.json_object()?;
    let result = project_usage(&body)?;
    context.human = Some(human_usage(&result));
    Ok(result)
}

pub fn human_usage(result: &Value) -> String {
    let mut text = format!(
        "Period: {} to {}\nRuns: {}\nInput tokens: {}\nOutput tokens: {}\nTotal price: {}\n",
        human_safe_scalar(result["period"]["start"].as_str().unwrap_or_default()),
        human_safe_scalar(result["period"]["end"].as_str().unwrap_or_default()),
        result["total_runs"],
        result["total_input_tokens"],
        result["total_output_tokens"],
        dollars_or_dash(&result["total_price_cents"]),
    );
    let daily = result["daily"].as_array().map_or(&[][..], Vec::as_slice);
    if daily.is_empty() {
        text.push_str("No runs in this period.\n");
    }
    for day in daily {
        let _ = writeln!(
            text,
            "{}  runs {}  input {}  output {}  price {}",
            human_safe_scalar(day["date"].as_str().unwrap_or_default()),
            day["runs"],
            day["input_tokens"],
            day["output_tokens"],
            dollars_or_dash(&day["price_cents"]),
        );
    }
    text
}

// -------------------------------------------------------------------- redeem

/// The code from `--code-file` (surrounding whitespace trimmed). Never echoed.
pub fn redeem_code(text: &str) -> Result<String, RunnerError> {
    let code = text.trim();
    if code.is_empty() {
        return Err(invalid(
            "--code-file is empty; put the credit code in the file (or pipe it to --code-file -)",
        ));
    }
    if !valid_text(code, CODE_MAX_CHARS) {
        return Err(invalid(format!(
            "--code-file must contain one credit code on one line (at most {CODE_MAX_CHARS} characters)"
        )));
    }
    Ok(code.to_owned())
}

pub fn project_redeem(body: &Map<String, Value>) -> Result<Value, RunnerError> {
    if body.get("ok") != Some(&Value::Bool(true)) {
        return Err(protocol("ok"));
    }
    let mut result = Map::new();
    result.insert("ok".into(), Value::Bool(true));
    result.insert(
        "amount_usd".into(),
        number(absent(body, "amount_usd"), "amount_usd")?,
    );
    for flag in ["already_redeemed", "credit_pending"] {
        match body.get(flag) {
            None | Some(Value::Null) => {}
            Some(Value::Bool(value)) => {
                result.insert(flag.into(), Value::Bool(*value));
            }
            Some(_) => return Err(protocol(flag)),
        }
    }
    if let Some(value) = body.get("available_usd").filter(|value| !value.is_null()) {
        result.insert("available_usd".into(), number(value, "available_usd")?);
    }
    if let Some(value) = body.get("message").filter(|value| !value.is_null()) {
        let message = free_text(value, "message")?;
        if !message.trim().is_empty() {
            result.insert("message".into(), json!(message));
        }
    }
    Ok(Value::Object(result))
}

fn redeem(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let source = context
        .option("--code-file")
        .ok_or_else(|| invalid("--code-file is required"))?
        .to_owned();
    let text = super::fs::read_text(
        &context.system.current_dir,
        &source,
        CODE_FILE_MAX_BYTES,
        "--code-file",
    )?;
    let code = redeem_code(&text)?;
    let request = http::Request::from_manifest(context.operation, 0, "/wallet/redeem")
        .json_body(&json!({"code": code}));
    // The code is a short bearer secret, so the plan never carries a digest
    // of the body (it would be brute-forceable offline): body fields are null.
    let planned = context.planned(0, "/wallet/redeem", &[], None);
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    let body = context.send(&request)?.json_object()?;
    let result = project_redeem(&body)?;
    context.human = Some(human_redeem(&result));
    Ok(result)
}

pub fn human_redeem(result: &Value) -> String {
    let amount = &result["amount_usd"];
    let mut text = if result["credit_pending"] == true {
        format!("Code claimed: ${amount} credit will appear shortly.\n")
    } else if result["already_redeemed"] == true {
        format!("Code already redeemed to this wallet: ${amount} credit.\n")
    } else {
        format!("Code redeemed: ${amount} credit added.\n")
    };
    if !result["available_usd"].is_null() {
        let _ = writeln!(text, "Available: ${}", result["available_usd"]);
    }
    text
}

// --------------------------------------------------------------------- topup

/// `--amount-cents`: a positive integer (the service enforces its minimum).
pub fn parse_amount_cents(value: &str) -> Result<u64, RunnerError> {
    value
        .parse::<u64>()
        .ok()
        .filter(|amount| {
            *amount >= 1 && *amount <= MAX_SAFE_INTEGER && value.bytes().all(|byte| byte.is_ascii_digit())
        })
        .ok_or_else(|| {
            invalid(format!(
                "--amount-cents must be a positive whole number of cents, for example 500 for $5.00; got {value_quoted}", value_quoted = crate::error::quote(value)))
        })
}

/// `--idempotency-key`: a lowercase canonical UUID.
pub fn valid_idempotency_key(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => *byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(byte),
        })
}

pub fn project_topup(body: &Map<String, Value>, key: &str) -> Result<Value, RunnerError> {
    let url = body
        .get("checkout_url")
        .and_then(Value::as_str)
        .filter(|url| {
            url.len() <= 4096
                && url
                    .strip_prefix("https://")
                    .is_some_and(|rest| !rest.is_empty() && !rest.chars().any(char::is_whitespace))
        })
        .ok_or_else(|| protocol("checkout_url"))?;
    let session = body
        .get("session_id")
        .and_then(Value::as_str)
        .filter(|session| valid_text(session, 256))
        .ok_or_else(|| protocol("session_id"))?;
    let amount = object(absent(body, "amount"), "amount")?;
    Ok(json!({
        "checkout_url": url,
        "session_id": session,
        "amount": {
            "credits_cents": integer(absent(amount, "credits_cents"), "amount.credits_cents", Some(0))?,
            "fee_cents": integer(absent(amount, "fee_cents"), "amount.fee_cents", Some(0))?,
            "total_cents": integer(absent(amount, "total_cents"), "amount.total_cents", Some(0))?,
        },
        "idempotencyKey": key,
    }))
}

fn topup(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let amount = parse_amount_cents(
        context
            .option("--amount-cents")
            .ok_or_else(|| invalid("--amount-cents is required"))?,
    )?;
    let given = context.option("--idempotency-key").map(str::to_owned);
    if let Some(key) = &given {
        if !valid_idempotency_key(key) {
            return Err(invalid(format!(
                "--idempotency-key must be a lowercase UUID such as 123e4567-e89b-42d3-a456-426614174000 (reuse the idempotencyKey of an earlier top-up); got {key_quoted}",
                key_quoted = crate::error::quote(key)
            )));
        }
    }
    let body = serde_json::to_vec(&json!({"amount_cents": amount})).expect("JSON serializes");
    let planned = context.planned(0, "/wallet/topup", &[], Some(&body));
    if let Gate::Preview(result) = context.gate(planned)? {
        return Ok(result);
    }
    // The key is minted only once the request will really be sent, so
    // --preview and CONFIRMATION_REQUIRED stay free and deterministic.
    let key = match given {
        Some(key) => key,
        None => context.uuid_v4()?,
    };
    let mut request = http::Request::from_manifest(context.operation, 0, "/wallet/topup")
        .header("Idempotency-Key", key.clone());
    request.body = Some(body);
    let outcome = context
        .send(&request)
        .and_then(|response| response.json_object())
        .and_then(|body| project_topup(&body, &key));
    match outcome {
        Ok(result) => {
            context.human = Some(context.localize(&human_topup(&result)));
            Ok(result)
        }
        // Retrying with the same key cannot create a second Checkout session.
        Err(error) => Err(error.with_detail("idempotencyKey", key)),
    }
}

pub fn human_topup(result: &Value) -> String {
    format!(
        "Checkout URL: {}\nCredits: {}\nFee: {}\nTotal: {}\nIdempotency key: {}\nOpen the URL in a browser to pay. The credit lands in the wallet once payment settles; check with `prose cli wallet balance`.\n",
        human_safe_scalar(result["checkout_url"].as_str().unwrap_or_default()),
        dollars_or_dash(&result["amount"]["credits_cents"]),
        dollars_or_dash(&result["amount"]["fee_cents"]),
        dollars_or_dash(&result["amount"]["total_cents"]),
        human_safe_scalar(result["idempotencyKey"].as_str().unwrap_or_default()),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn map(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn balance_drops_identity_and_nanos() {
        let projected = project_balance(&map(json!({
            "customer_id": "cus_x",
            "balance": {"available_cents": 3337, "available_dollars": "33.37", "available_nanos": 1,
                        "posted_cents": 3337, "posted_dollars": "33.37",
                        "reserved_cents": 0, "reserved_dollars": "0.00", "reserved_nanos": 0}
        })))
        .unwrap();
        assert_eq!(
            projected,
            json!({"available_cents": 3337, "available_dollars": "33.37", "posted_cents": 3337,
                   "posted_dollars": "33.37", "reserved_cents": 0, "reserved_dollars": "0.00"})
        );
        assert!(project_balance(&map(json!({"balance": {"available_cents": "1"}}))).is_err());
    }

    #[test]
    fn events_limit_is_bounded() {
        assert_eq!(parse_events_limit(None).unwrap(), 20);
        assert_eq!(parse_events_limit(Some("100")).unwrap(), 100);
        for bad in ["0", "101", "+5", "-1", "2.0", "", "x"] {
            assert!(parse_events_limit(Some(bad)).is_err(), "{bad}");
        }
    }

    #[test]
    fn events_projection_sanitizes_text_and_rejects_bad_identifiers() {
        let (result, next) = project_events(&map(json!({
            "customer_id": "cus_x",
            "events": [{"id": "charge:1", "type": "run_charge", "occurred_at": "2026-09-23T20:31:43.779Z",
                        "description": "a\nb", "amount_cents": -2, "balance_after_cents": 5.0, "ref": null}],
            "next_before": "charge:1", "note": "n"
        })))
        .unwrap();
        assert_eq!(next.as_deref(), Some("charge:1"));
        assert_eq!(result["events"][0]["description"], "a b");
        assert_eq!(result["events"][0]["balance_after_cents"], json!(5));
        assert!(result.get("customer_id").is_none());
        let bad = json!({"events": [{"id": "a\u{7}", "type": "t", "occurred_at": "2026-09-23T00:00:00Z",
                                      "description": "", "amount_cents": 1, "balance_after_cents": 1}],
                         "next_before": null});
        assert!(project_events(&map(bad)).is_err());
    }

    #[test]
    fn usage_window_is_validated() {
        let now = "2026-09-23T12:00:00.000Z";
        assert!(usage_window(Some("2026-02-30"), None, now).is_err());
        assert!(usage_window(None, Some("2026-09-31"), now).is_err());
        assert!(usage_window(Some("2025-02-29"), Some("2025-03-01"), now).is_err());
        assert!(usage_window(Some("2024-02-29"), Some("2024-03-01"), now).is_ok());
        assert!(usage_window(Some("2026-13-01"), None, now).is_err());
        assert!(usage_window(Some("2026-09-02"), Some("2026-09-01"), now).is_err());
        // Lone bounds are checked against the server-defaulted other bound.
        assert!(usage_window(Some("2026-09-23"), None, now).is_ok());
        assert!(usage_window(Some("2026-09-24"), None, now).is_err());
        assert!(usage_window(None, Some("2026-08-24"), now).is_ok());
        assert!(usage_window(None, Some("2026-08-23"), now).is_err());
        // An explicit future or past window is a valid (empty) range.
        assert!(usage_window(Some("2030-01-01"), Some("2030-01-02"), now).is_ok());
        assert_eq!(
            usage_window(Some("2026-09-01"), Some("2026-09-02"), now).unwrap(),
            vec![
                ("start".into(), "2026-09-01".into()),
                ("end".into(), "2026-09-02".into())
            ]
        );
    }

    #[test]
    fn redeem_code_is_trimmed_single_line() {
        assert_eq!(redeem_code(" Q7RC9-V5K2M\n").unwrap(), "Q7RC9-V5K2M");
        assert!(redeem_code("\n").is_err());
        assert!(redeem_code("a\nb").is_err());
    }

    #[test]
    fn topup_inputs_and_projection() {
        assert_eq!(parse_amount_cents("500").unwrap(), 500);
        for bad in ["0", "-5", "5.00", "1e3", "9007199254740992", ""] {
            assert!(parse_amount_cents(bad).is_err(), "{bad}");
        }
        assert!(valid_idempotency_key(
            "123e4567-e89b-42d3-a456-426614174000"
        ));
        assert!(!valid_idempotency_key(
            "123E4567-E89B-42D3-A456-426614174000"
        ));
        let body = map(
            json!({"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_1",
                              "session_id": "cs_test_1",
                              "amount": {"credits_cents": 500, "fee_cents": 60, "total_cents": 560}}),
        );
        let key = "123e4567-e89b-42d3-a456-426614174000";
        assert_eq!(project_topup(&body, key).unwrap()["idempotencyKey"], key);
        let insecure = map(json!({"checkout_url": "http://x", "session_id": "s",
                                  "amount": {"credits_cents": 1, "fee_cents": 0, "total_cents": 1}}));
        assert!(project_topup(&insecure, key).is_err());
    }
}
