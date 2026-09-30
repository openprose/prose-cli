//! Service discovery: `cli service status`, `cli model list`,
//! `cli example list|show` and `cli repo list`.
//!
//! Each handler validates the service response against the fields it consumes
//! (A3 acceptance) and projects a closed result
//! (`shared/schemas/service/discovery.schema.json`). A malformed success is
//! `SERVICE_PROTOCOL_INVALID` with a `reason` naming the first bad field; the
//! fields are checked in a fixed order, identical in the Bun product
//! (`cli/bun/src/core/service/discovery.ts`). Decisions: docs/service/discovery.md.
use super::{Context, fs, http, render};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fmt::Write as _;
use std::path::Path;

/// Dispatches one discovery operation.
pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    match context.operation["id"].as_str().unwrap_or_default() {
        "service.status" => service_status(context),
        "service.triage" => super::triage::execute(context),
        "model.list" => model_list(context),
        "example.list" => example_list(context),
        "example.show" => example_show(context),
        "repo.list" => repo_list(context),
        _ => context.not_implemented(),
    }
}

// ---------------------------------------------------------------------------
// Response validation

/// Names the response being validated (`GET /health`) for protocol errors.
struct Shape<'a> {
    what: &'a str,
}

impl Shape<'_> {
    fn fail(&self, field: &str) -> RunnerError {
        RunnerError::catalog(ErrorCode::ServiceProtocolInvalid).with_detail(
            "reason",
            format!("unexpected {} response: {field}", self.what),
        )
    }

    fn object(&self, response: &http::Response) -> Result<Map<String, Value>, RunnerError> {
        response.json_object().map_err(|_| self.fail("body"))
    }

    fn field<'v>(
        &self,
        object: &'v Map<String, Value>,
        key: &str,
        path: &str,
    ) -> Result<&'v Value, RunnerError> {
        object.get(key).ok_or_else(|| self.fail(path))
    }

    /// A string of at most `max` code points without C0 or DEL characters.
    fn text(&self, value: &Value, max: usize, path: &str) -> Result<String, RunnerError> {
        match value.as_str() {
            Some(text) if plain(text, max) => Ok(text.to_owned()),
            _ => Err(self.fail(path)),
        }
    }

    fn model(&self, value: &Value, path: &str) -> Result<String, RunnerError> {
        match value.as_str() {
            Some(text) if model_id(text) => Ok(text.to_owned()),
            _ => Err(self.fail(path)),
        }
    }

    fn list<'v>(
        &self,
        value: &'v Value,
        max: usize,
        path: &str,
    ) -> Result<&'v [Value], RunnerError> {
        match value.as_array() {
            Some(values) if values.len() <= max => Ok(values),
            _ => Err(self.fail(path)),
        }
    }

    fn map<'v>(&self, value: &'v Value, path: &str) -> Result<&'v Map<String, Value>, RunnerError> {
        value.as_object().ok_or_else(|| self.fail(path))
    }
}

fn plain(text: &str, max: usize) -> bool {
    text.chars().count() <= max
        && !text
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
}

/// `^[a-z0-9][a-z0-9.-]{0,63}$`
fn model_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-')
        })
}

/// `^[a-z0-9][a-z0-9-]{0,63}$`
fn slug(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

fn integer(value: &Value) -> Option<i64> {
    value.as_i64()
}

// ---------------------------------------------------------------------------
// service status

/// `cli service status`: whether the service answers and the models this
/// account can run. Nothing else of `/health` is read or shown.
fn service_status(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let response = context.send(&http::Request::from_manifest(
        context.operation,
        0,
        "/health",
    ))?;
    let mut result = health(&response)?;
    result.insert(
        "environments".into(),
        json!(health_environments(&response)?),
    );
    let result = Value::Object(result);
    context.human = Some(status_human(&result));
    Ok(result)
}

/// The environment ids /health offers (`environments.available`), the ones
/// `run submit --environment` and `run quote --environment` accept; empty
/// when /health lists none (mirrors Bun `healthEnvironments`).
fn health_environments(response: &http::Response) -> Result<Vec<String>, RunnerError> {
    let shape = Shape {
        what: "service status",
    };
    let body = shape.object(response)?;
    let Some(list) = body
        .get("environments")
        .and_then(Value::as_object)
        .and_then(|environments| environments.get("available"))
    else {
        return Ok(Vec::new());
    };
    shape
        .list(list, 64, "environments.available")?
        .iter()
        .map(|item| match item.as_str() {
            Some(id) if environment_id(id) => Ok(id.to_owned()),
            _ => Err(shape.fail("environments.available")),
        })
        .collect()
}

/// `^[a-z0-9][a-z0-9_-]{0,63}$`
fn environment_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        })
}

/// `GET /health` projected as `cli service status` does (shared with triage):
/// `status`, `models` and `default_model`, checked in that order. Every
/// other field is ignored.
pub(super) fn health(response: &http::Response) -> Result<Map<String, Value>, RunnerError> {
    let shape = Shape {
        what: "service status",
    };
    let body = shape.object(response)?;
    let mut result = Map::new();
    let status = shape.text(shape.field(&body, "status", "status")?, 32, "status")?;
    result.insert("status".into(), json!(status));
    let models = shape
        .list(shape.field(&body, "models", "models")?, 64, "models")?
        .iter()
        .map(|item| shape.model(item, "models"))
        .collect::<Result<Vec<_>, _>>()?;
    result.insert("models".into(), json!(models));
    let default = shape.model(
        shape.field(&body, "default_model", "default_model")?,
        "default_model",
    )?;
    result.insert("default_model".into(), json!(default));
    Ok(result)
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(human_safe_scalar)
                .collect()
        })
        .unwrap_or_default()
}

fn joined(items: &[String]) -> String {
    if items.is_empty() {
        "(none)".into()
    } else {
        items.join(", ")
    }
}

fn status_human(result: &Value) -> String {
    let text_of = |value: &Value| human_safe_scalar(value.as_str().unwrap_or_default());
    let mut text = String::new();
    let _ = writeln!(text, "Service: {}", text_of(&result["status"]));
    let _ = writeln!(
        text,
        "Models: {} (default {})",
        joined(&strings(&result["models"])),
        text_of(&result["default_model"])
    );
    let environments = strings(&result["environments"]);
    if !environments.is_empty() {
        let _ = writeln!(text, "Environments: {}", environments.join(", "));
    }
    text.push_str(&render::next_line(&["service", "triage"]));
    text
}

// ---------------------------------------------------------------------------
// model list

fn model_list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let response = context.send(&http::Request::from_manifest(
        context.operation,
        0,
        "/models",
    ))?;
    let shape = Shape { what: "model list" };
    let body = shape.object(&response)?;
    let result = project_models(&shape, &body)?;
    context.human = Some(models_human(&result));
    Ok(result)
}

/// The models the service lists for this account, its default and, when the
/// service sends one, the `catalog` of every model with its status (see
/// [`catalog`]). Nothing else of `/models` is read or shown.
fn project_models(shape: &Shape<'_>, body: &Map<String, Value>) -> Result<Value, RunnerError> {
    let models = shape
        .list(shape.field(body, "models", "models")?, 64, "models")?
        .iter()
        .map(|item| shape.model(item, "models"))
        .collect::<Result<Vec<_>, _>>()?;
    let default = shape.model(
        shape.field(body, "default_model", "default_model")?,
        "default_model",
    )?;
    let mut result = json!({"models": models, "default_model": default});
    if let Some(catalog) = catalog(body) {
        result["catalog"] = Value::Array(catalog);
    }
    Ok(result)
}

/// The catalog status of a model that unlocks with any wallet top-up.
const PREMIUM: &str = "paid_top_up";
/// The catalog status of a model the service still accepts but no longer offers.
const HIDDEN: &str = "hidden";
/// The catalog status of a retired model; never shown.
const DEPRECATED: &str = "deprecated";

/// `/models` `catalog`, projected through an explicit allowlist: each entry's
/// `id` and `status`, plus `tier`, `summary` and `successor` when the service
/// sends them valid. Lenient, because older servers send no catalog and a
/// newer one may add statuses: a missing or non-list catalog is `None`, the
/// first 64 entries are read, an entry without a model `id` or a status
/// token, a repeated id or a `deprecated` model is skipped, and an invalid
/// optional field is left out (identical in the Bun product).
fn catalog(body: &Map<String, Value>) -> Option<Vec<Value>> {
    let entries = body.get("catalog")?.as_array()?;
    let mut projected: Vec<Value> = Vec::new();
    for entry in entries.iter().take(64) {
        let Some(entry) = entry.as_object() else {
            continue;
        };
        let (Some(id), Some(status)) = (
            entry
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| model_id(id)),
            entry
                .get("status")
                .and_then(Value::as_str)
                .filter(|status| token(status)),
        ) else {
            continue;
        };
        if status == DEPRECATED || projected.iter().any(|known| known["id"] == id) {
            continue;
        }
        let mut item = json!({"id": id, "status": status});
        if let Some(tier) = entry
            .get("tier")
            .and_then(Value::as_str)
            .filter(|tier| token(tier))
        {
            item["tier"] = json!(tier);
        }
        if let Some(summary) = entry
            .get("summary")
            .and_then(Value::as_str)
            .filter(|summary| !summary.trim().is_empty() && plain(summary, 512))
        {
            item["summary"] = json!(summary);
        }
        if let Some(successor) = entry
            .get("successor")
            .and_then(Value::as_str)
            .filter(|successor| model_id(successor))
        {
            item["successor"] = json!(successor);
        }
        projected.push(item);
    }
    Some(projected)
}

/// A catalog status or tier: `^[a-z][a-z0-9_]{0,31}$`.
fn token(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=32).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_')
}

/// The `cli` words of the top-up preview that unlocks premium models.
const TOPUP: [&str; 5] = ["wallet", "topup", "--amount-cents", "500", "--preview"];

/// Readable `model list` output, ending with a copyable `Next:` command.
/// Without a catalog: one model per line. With one: the models this account
/// can run, the premium ones any wallet top-up unlocks, and the older ids the
/// service still accepts, each in its own section.
fn models_human(result: &Value) -> String {
    let default = result["default_model"].as_str().unwrap_or_default();
    let listed = result["models"].as_array().map_or(&[][..], Vec::as_slice);
    let Some(catalog) = result["catalog"].as_array() else {
        let mut text = String::new();
        if listed.is_empty() {
            text.push_str("No models.\n");
        }
        for model in listed {
            let model = model.as_str().unwrap_or_default();
            let marker = if model == default { " (default)" } else { "" };
            let _ = writeln!(text, "{}{marker}", human_safe_scalar(model));
        }
        if !listed.iter().any(|model| model == default) {
            let _ = writeln!(text, "Default: {}", human_safe_scalar(default));
        }
        text.push_str(&render::next_line(&["run", "quote"]));
        return text;
    };
    let with_status = |status: &str| {
        catalog
            .iter()
            .filter(|entry| entry["status"] == status)
            .collect::<Vec<_>>()
    };
    let mut text = String::from("Available:\n");
    if listed.is_empty() {
        text.push_str("  (none)\n");
    }
    for model in listed {
        let model = model.as_str().unwrap_or_default();
        let marker = if model == default { " (default)" } else { "" };
        let _ = writeln!(text, "  {}{marker}", human_safe_scalar(model));
    }
    if !listed.iter().any(|model| model == default) {
        let _ = writeln!(text, "  Default: {}", human_safe_scalar(default));
    }
    let premium = with_status(PREMIUM);
    if !premium.is_empty() {
        text.push_str("Premium \u{2014} unlocks with any wallet top-up:\n");
        for entry in premium {
            let id = human_safe_scalar(entry["id"].as_str().unwrap_or_default());
            match entry["summary"].as_str() {
                Some(summary) => {
                    let _ = writeln!(text, "  {id}: {}", human_safe_scalar(summary));
                }
                None => {
                    let _ = writeln!(text, "  {id}");
                }
            }
        }
        text.push_str(&render::next_line(&TOPUP));
    }
    let hidden = with_status(HIDDEN);
    if !hidden.is_empty() {
        text.push_str("Also accepted (not recommended):\n");
        for entry in hidden {
            let id = human_safe_scalar(entry["id"].as_str().unwrap_or_default());
            match entry["successor"].as_str() {
                Some(successor) => {
                    let _ = writeln!(text, "  {id} \u{2192} use {}", human_safe_scalar(successor));
                }
                None => {
                    let _ = writeln!(text, "  {id}");
                }
            }
        }
    }
    text.push_str(&render::next_line(&["run", "quote"]));
    text
}

/// Service codes that refuse a premium model until the wallet has been
/// topped up: `paid_top_up_required` (HTTP 402), and `paid_model_required`
/// (HTTP 403) from older servers.
const PAID_CODES: [&str; 2] = ["paid_top_up_required", "paid_model_required"];

/// Explains a classified service refusal of a premium model: the body's
/// `model`, `tier` and `summary` become `details.model`, `details.tier` and
/// `details.reason`, and the Action (also `details.suggestedArgv`) previews a
/// wallet top-up. Any other error is returned unchanged.
pub(super) fn paid_model_refusal(
    error: RunnerError,
    response: &http::Response,
    mode: crate::OutputMode,
    credential: Option<&str>,
) -> RunnerError {
    let Ok(Value::Object(body)) = serde_json::from_slice::<Value>(&response.body) else {
        return error;
    };
    if !body
        .get("code")
        .and_then(Value::as_str)
        .is_some_and(|code| PAID_CODES.contains(&code))
    {
        return error;
    }
    let text = |key: &str| body.get(key).and_then(Value::as_str);
    let summary =
        text("summary").and_then(|summary| render::sanitize_service_message(summary, credential));
    premium_refusal(
        error,
        text("model").filter(|model| model_id(model)),
        text("tier").filter(|tier| token(tier)),
        summary.as_deref(),
        mode,
    )
}

/// The premium-model refusal `cli model list` predicts before any request:
/// `Some` when `/models` lists `model` in its catalog as a premium model.
pub(super) fn premium_in_catalog(
    body: &Map<String, Value>,
    model: &str,
    mode: crate::OutputMode,
) -> Option<RunnerError> {
    let entries = catalog(body)?;
    let entry = entries.iter().find(|entry| entry["id"] == model)?;
    (entry["status"] == PREMIUM).then(|| {
        premium_refusal(
            RunnerError::catalog(ErrorCode::ServicePremiumModelLocked),
            Some(model),
            entry["tier"].as_str(),
            entry["summary"].as_str(),
            mode,
        )
    })
}

/// Whether the `/models` catalog lists `model` as one the service accepts
/// (any status but premium; retired models are never listed).
pub(super) fn accepted_in_catalog(body: &Map<String, Value>, model: &str) -> bool {
    catalog(body).is_some_and(|entries| {
        entries
            .iter()
            .any(|entry| entry["id"] == model && entry["status"] != PREMIUM)
    })
}

fn premium_refusal(
    error: RunnerError,
    model: Option<&str>,
    tier: Option<&str>,
    summary: Option<&str>,
    mode: crate::OutputMode,
) -> RunnerError {
    let reason = match (model, summary) {
        (Some(model), Some(summary)) => format!("{model} is a premium model: {summary}"),
        (Some(model), None) => {
            format!("{model} is a premium model; it unlocks with any wallet top-up.")
        }
        (None, Some(summary)) => format!("This model is premium: {summary}"),
        (None, None) => "This model is premium; it unlocks with any wallet top-up.".to_owned(),
    };
    let mut error = error.with_detail("reason", reason);
    if let Some(model) = model {
        error = error.with_detail("model", model);
    }
    if let Some(tier) = tier {
        error = error.with_detail("tier", tier);
    }
    let argv = render::follow_up_argv(mode, &TOPUP);
    error.action = format!(
        "Top up the wallet with any amount to unlock premium models; preview a top-up with `{}`.",
        render::argv_text(&argv)
    );
    error.with_detail("suggestedArgv", json!(argv))
}

// ---------------------------------------------------------------------------
// example list / show

const EXAMPLE_PREFIX: &str = "/examples/private/";

/// The same-origin request path for an example `source`, or `None` when the
/// source is outside the selected origin or is not a private-example route.
///
/// Admitted: `/examples/private/<name>` (relative) or the same path under the
/// exact selected origin, where `<name>` matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
pub fn example_source_path(source: &str, origin: &str) -> Option<String> {
    let path = if source.starts_with('/') && !source.starts_with("//") {
        source
    } else {
        source
            .strip_prefix(origin)
            .filter(|rest| rest.starts_with('/'))?
    };
    let name = path.strip_prefix(EXAMPLE_PREFIX)?;
    let bytes = name.as_bytes();
    let valid = (1..=128).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'));
    valid.then(|| path.to_owned())
}

fn project_examples(
    shape: &Shape<'_>,
    body: &Map<String, Value>,
    origin: &str,
) -> Result<Vec<Value>, RunnerError> {
    shape
        .list(shape.field(body, "examples", "examples")?, 256, "examples")?
        .iter()
        .map(|item| {
            let item = shape.map(item, "examples")?;
            let id = shape.field(item, "id", "examples.id")?;
            let id = match id.as_str() {
                Some(id) if slug(id) => id.to_owned(),
                _ => return Err(shape.fail("examples.id")),
            };
            let label = shape.text(
                shape.field(item, "label", "examples.label")?,
                256,
                "examples.label",
            )?;
            let source = shape.field(item, "source", "examples.source")?;
            let source = source
                .as_str()
                .ok_or_else(|| shape.fail("examples.source"))?;
            let path = example_source_path(source, origin);
            // The web page of an example published outside the service, when
            // the service names one: an https URL without spaces or controls.
            let web = item
                .get("web_url")
                .and_then(Value::as_str)
                .filter(|url| web_url(url))
                .map_or(Value::Null, |url| json!(url));
            // Where a person can read an example `example show` cannot fetch
            // (EXAMPLE_NOT_VIEWABLE details.webUrl): the service's web page,
            // else the source itself when it is an https address.
            let read_at = match (&path, web.as_str()) {
                (None, Some(url)) => json!(url),
                (None, None) if web_url(source) => json!(source),
                _ => Value::Null,
            };
            Ok(json!({
                "id": id,
                "label": label,
                "source": path,
                "web_url": web,
                "read_at": read_at,
            }))
        })
        .collect()
}

/// An example's web page URL: `https://` plus a host, at most 2048
/// printable ASCII characters (identical in both ports).
fn web_url(url: &str) -> bool {
    url.len() <= 2048
        && url.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
        && url
            .strip_prefix("https://")
            .is_some_and(|rest| !rest.is_empty() && !rest.starts_with('/'))
}

fn fetch_examples(context: &mut Context<'_>) -> Result<Vec<Value>, RunnerError> {
    let response = context.send(&http::Request::from_manifest(
        context.operation,
        0,
        "/examples/private",
    ))?;
    let shape = Shape {
        what: "example list",
    };
    let body = shape.object(&response)?;
    let origin = context.environment.origin.clone();
    project_examples(&shape, &body, &origin)
}

fn example_list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let examples = fetch_examples(context)?;
    let mut text = String::new();
    if examples.is_empty() {
        text.push_str("No examples.\n");
    }
    for example in &examples {
        let suffix = match (example["source"].is_null(), example["web_url"].as_str()) {
            (false, _) => String::new(),
            (true, Some(url)) => format!(" (view on the web: {})", human_safe_scalar(url)),
            (true, None) => " (view on the web)".to_owned(),
        };
        let _ = writeln!(
            text,
            "{}: {}{suffix}",
            human_safe_scalar(example["id"].as_str().unwrap_or_default()),
            human_safe_scalar(example["label"].as_str().unwrap_or_default())
        );
    }
    context.human = Some(text);
    // The public listing: each example's id and label; one that `cli example
    // show` cannot fetch is `viewOnWeb`, with its `webUrl` when the service
    // names one. The service route stays internal.
    let listed = examples
        .iter()
        .map(|example| {
            let mut item = json!({"id": example["id"], "label": example["label"]});
            if example["source"].is_null() {
                item["viewOnWeb"] = json!(true);
                if let Some(url) = example["web_url"].as_str() {
                    item["webUrl"] = json!(url);
                }
            }
            item
        })
        .collect::<Vec<_>>();
    Ok(json!({"examples": listed}))
}

/// The shared text rule for example bytes: UTF-8 without C0 controls other
/// than TAB, LF and CR, and without DEL.
fn example_text(bytes: &[u8]) -> Option<&str> {
    let text = std::str::from_utf8(bytes).ok()?;
    let allowed = |character: char| {
        !(character <= '\u{1f}' || character == '\u{7f}') || matches!(character, '\t' | '\n' | '\r')
    };
    text.chars().all(allowed).then_some(text)
}

/// Fails early (before any request) when `--output-file` cannot be created;
/// the rule and messages are [`fs::check_new_file`].
fn check_output_file(cwd: &Path, value: &str) -> Result<(), RunnerError> {
    fs::check_new_file(cwd, value).map(|_| ())
}

/// The listed example `given` names: its id, else the id or label compared
/// case-insensitively; a label must name exactly one example.
pub fn find_example<'a>(examples: &'a [Value], given: &str) -> Option<&'a Value> {
    if let Some(exact) = examples.iter().find(|item| item["id"] == given) {
        return Some(exact);
    }
    let key = given.trim().to_lowercase();
    if let Some(by_id) = examples.iter().find(|item| {
        item["id"]
            .as_str()
            .is_some_and(|id| id.to_lowercase() == key)
    }) {
        return Some(by_id);
    }
    let by_label = examples
        .iter()
        .filter(|item| {
            item["label"]
                .as_str()
                .is_some_and(|label| label.trim().to_lowercase() == key)
        })
        .collect::<Vec<_>>();
    match by_label.as_slice() {
        [only] => Some(only),
        _ => None,
    }
}

fn example_show(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let given = context.argument("NAME").unwrap_or_default().to_owned();
    if !super::render::valid_text(&given, 256) {
        return Err(RunnerError::invocation(format!(
            "example name {given_quoted} is invalid; use an id from `{}`",
            context.command("example list"),
            given_quoted = crate::error::quote(&given)
        )));
    }
    let output_file = context.option("--output-file").map(str::to_owned);
    if let Some(file) = &output_file {
        check_output_file(&context.system.current_dir, file)?;
    }
    let examples = fetch_examples(context)?;
    let Some(example) = find_example(&examples, &given) else {
        let lower = given.to_lowercase();
        let near = super::did_you_mean(
            &lower,
            examples.iter().filter_map(|item| item["id"].as_str()),
        )
        .map(str::to_owned);
        let list = context.command("example list");
        let error = RunnerError::catalog(ErrorCode::ServiceResourceNotFound);
        return Err(match near {
            None => error.with_detail(
                "reason",
                format!("no example named {given_quoted}; list the available ids with `{list}`", given_quoted = crate::error::quote(&given)),
            ),
            Some(near) => error
                .with_detail(
                    "reason",
                    format!(
                        "no example named {given_quoted}; did you mean {near_quoted}? Show it with `{}`, or list the available ids with `{list}`",
                        context.command(&format!("example show {near}")), given_quoted = crate::error::quote(&given), near_quoted = crate::error::quote(&near)),
                )
                .with_detail(
                    "suggestedArgv",
                    json!(context.follow_up_argv(&["example", "show", &near])),
                ),
        });
    };
    let example = example.clone();
    let name = example["id"].as_str().unwrap_or_default().to_owned();
    if name != given && context.mode == crate::OutputMode::Human {
        let note = format!(
            "Using example {name} ({}) for {}.\n",
            human_safe_scalar(example["label"].as_str().unwrap_or_default()),
            human_safe_scalar(&given)
        );
        let _ = std::io::Write::write_all(context.err, note.as_bytes());
    }
    let label = example["label"].clone();
    let Some(path) = example["source"].as_str().map(str::to_owned) else {
        let mut error = RunnerError::catalog(ErrorCode::ExampleNotViewable).with_detail(
            "reason",
            format!(
                "example {} is published outside the OpenProse service, so its source cannot be shown here; view it on the web",
                crate::error::quote(&name)
            ),
        );
        if let Some(url) = example["read_at"].as_str() {
            error = error.with_detail("webUrl", url);
        }
        return Err(error);
    };
    let response = context.send(&http::Request::from_manifest(context.operation, 1, path))?;
    let shape = Shape { what: "example" };
    let text = example_text(&response.body)
        .ok_or_else(|| shape.fail("body"))?
        .to_owned();
    let bytes = response.body.len();
    let sha256 = format!("{:x}", Sha256::digest(&response.body));
    let mut result = json!({
        "id": name,
        "label": label,
        "bytes": bytes,
        "sha256": sha256,
        "written": output_file.is_some(),
    });
    if let Some(file) = &output_file {
        fs::write_new_file(&context.system.current_dir, file, &response.body)?;
        context.human = Some(format!(
            "Wrote {bytes} bytes to {} (sha256 {sha256})\n",
            human_safe_scalar(file)
        ));
    } else {
        result["content"] = json!(text);
        context.human = Some(text);
    }
    Ok(result)
}

// ---------------------------------------------------------------------------
// repo list

/// Flattens `{installations: [{repos: [...]}]}` into `repositories`, sorted by
/// `full_name` then `id`; a repository listed by two installations is kept once.
fn project_repositories(
    shape: &Shape<'_>,
    body: &Map<String, Value>,
) -> Result<Vec<Value>, RunnerError> {
    let installations = shape.list(
        shape.field(body, "installations", "installations")?,
        usize::MAX,
        "installations",
    )?;
    let mut repositories: Vec<(String, i64, Value)> = Vec::new();
    for installation in installations {
        let installation = shape.map(installation, "installations")?;
        let repos = shape.list(
            shape.field(installation, "repos", "installations.repos")?,
            usize::MAX,
            "installations.repos",
        )?;
        for repo in repos {
            let repo = shape.map(repo, "installations.repos")?;
            let id = integer(shape.field(repo, "id", "installations.repos.id")?)
                .ok_or_else(|| shape.fail("installations.repos.id"))?;
            let name = shape.text(
                shape.field(repo, "name", "installations.repos.name")?,
                100,
                "installations.repos.name",
            )?;
            let full_name = shape.text(
                shape.field(repo, "full_name", "installations.repos.full_name")?,
                200,
                "installations.repos.full_name",
            )?;
            let private = shape
                .field(repo, "private", "installations.repos.private")?
                .as_bool()
                .ok_or_else(|| shape.fail("installations.repos.private"))?;
            let mut projected =
                json!({"id": id, "name": name, "full_name": full_name, "private": private});
            match repo.get("default_branch") {
                None | Some(Value::Null) => {}
                Some(branch) => {
                    projected["default_branch"] =
                        json!(shape.text(branch, 256, "installations.repos.default_branch")?);
                }
            }
            if !repositories.iter().any(|(_, known, _)| *known == id) {
                repositories.push((full_name, id, projected));
            }
        }
    }
    if repositories.len() > 10_000 {
        return Err(shape.fail("installations.repos"));
    }
    repositories.sort_by(|left, right| left.0.cmp(&right.0).then(left.1.cmp(&right.1)));
    Ok(repositories
        .into_iter()
        .map(|(_, _, value)| value)
        .collect())
}

fn repo_list(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let response = context.send(&http::Request::from_manifest(
        context.operation,
        0,
        "/repos",
    ))?;
    let shape = Shape {
        what: "repository list",
    };
    let body = shape.object(&response)?;
    let repositories = project_repositories(&shape, &body)?;
    let mut text = String::new();
    if repositories.is_empty() {
        text.push_str("No repositories.\n");
    }
    for repo in &repositories {
        let visibility = if repo["private"] == true {
            "private"
        } else {
            "public"
        };
        let branch = repo["default_branch"]
            .as_str()
            .map(|branch| format!(", default branch {}", human_safe_scalar(branch)))
            .unwrap_or_default();
        let _ = writeln!(
            text,
            "{} ({visibility}{branch})",
            human_safe_scalar(repo["full_name"].as_str().unwrap_or_default())
        );
    }
    context.human = Some(text);
    Ok(json!({"repositories": repositories}))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SHAPE: Shape<'static> = Shape { what: "GET /test" };

    fn object(value: Value) -> Map<String, Value> {
        let Value::Object(map) = value else {
            panic!("object expected")
        };
        map
    }

    fn reason(error: &RunnerError) -> String {
        error.details.as_ref().unwrap()["reason"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    #[test]
    fn example_sources_follow_only_same_origin_private_routes() {
        let origin = super::super::PRODUCTION_ORIGIN;
        assert_eq!(
            example_source_path("/examples/private/private-example", origin).as_deref(),
            Some("/examples/private/private-example")
        );
        assert_eq!(
            example_source_path(&format!("{origin}/examples/private/a.prose.md"), origin)
                .as_deref(),
            Some("/examples/private/a.prose.md")
        );
        for refused in [
            "https://examples.example.org/examples/agent-native.prose.md",
            "https://run-prose-production.openprose.workers.dev.evil.example/examples/private/x",
            "//evil.example/examples/private/x",
            "/examples/private/../wallet",
            "/examples/private/",
            "/examples/private/a/b",
            "/examples/private/x?y=1",
            "/examples/public/x",
            "examples/private/x",
        ] {
            assert_eq!(example_source_path(refused, origin), None, "{refused}");
        }
    }

    #[test]
    fn health_keeps_only_status_and_models() {
        let response = http::Response {
            status: 200,
            headers: std::collections::BTreeMap::new(),
            body: json!({"status": "ok", "models": ["model-sol"], "default_model": "model-sol",
                         "version": "9.9.9", "extra": {"x": 1}})
            .to_string()
            .into_bytes(),
        };
        assert_eq!(
            Value::Object(health(&response).unwrap()),
            json!({"status": "ok", "models": ["model-sol"], "default_model": "model-sol"})
        );
        let missing = http::Response {
            body: json!({"status": "ok", "models": []})
                .to_string()
                .into_bytes(),
            ..response
        };
        assert_eq!(
            reason(&health(&missing).unwrap_err()),
            "unexpected service status response: default_model"
        );
        let text =
            status_human(&json!({"status": "ok", "models": ["a", "b"], "default_model": "a"}));
        assert_eq!(
            text,
            "Service: ok\nModels: a, b (default a)\nNext: prose cli service triage\n"
        );
    }

    #[test]
    fn models_keep_only_the_listed_models_and_default() {
        let body = object(json!({
            "models": ["model-astra", "model-sol"],
            "default_model": "model-sol",
            "retired": ["old-model"],
        }));
        let models = project_models(&SHAPE, &body).unwrap();
        assert_eq!(
            models,
            json!({"models": ["model-astra", "model-sol"], "default_model": "model-sol"})
        );
        assert_eq!(
            models_human(&models),
            "model-astra\nmodel-sol (default)\nNext: prose cli run quote\n"
        );
    }

    #[test]
    fn catalog_is_allowlisted_and_refuses_premium_models() {
        let body = object(json!({
            "models": ["model-sol"],
            "default_model": "model-sol",
            "catalog": [
                {"id": "model-astra", "status": "paid_top_up", "tier": "premium", "summary": "Big.", "price": 9},
                {"id": "model-sol", "status": "available"},
                {"id": "model-old", "status": "deprecated"},
                {"id": "model-sol-legacy", "status": "hidden", "successor": "model-sol"},
                {"id": "model-sol", "status": "hidden"},
                {"id": "BAD", "status": "available"},
            ],
        }));
        assert_eq!(
            project_models(&SHAPE, &body).unwrap()["catalog"],
            json!([
                {"id": "model-astra", "status": "paid_top_up", "tier": "premium", "summary": "Big."},
                {"id": "model-sol", "status": "available"},
                {"id": "model-sol-legacy", "status": "hidden", "successor": "model-sol"},
            ])
        );
        assert!(accepted_in_catalog(&body, "model-sol-legacy"));
        assert!(!accepted_in_catalog(&body, "model-astra"));
        assert!(!accepted_in_catalog(&body, "model-old"));
        let error = premium_in_catalog(&body, "model-astra", crate::OutputMode::Human).unwrap();
        assert_eq!(error.code, ErrorCode::ServicePremiumModelLocked);
        assert_eq!(reason(&error), "model-astra is a premium model: Big.");
        assert!(premium_in_catalog(&body, "model-sol", crate::OutputMode::Human).is_none());
        let legacy = object(json!({"models": ["model-sol"], "default_model": "model-sol"}));
        assert!(
            project_models(&SHAPE, &legacy)
                .unwrap()
                .get("catalog")
                .is_none()
        );
    }

    #[test]
    fn repositories_are_flattened_sorted_and_deduplicated() {
        let body = object(json!({"installations": [
            {"owner": "b", "owner_type": "User", "repos": [{"id": 2, "name": "z", "full_name": "b/z", "private": true}]},
            {"owner": "a", "owner_type": "Organization", "repos": [
                {"id": 1, "name": "y", "full_name": "a/y", "private": false, "default_branch": "main"},
                {"id": 2, "name": "z", "full_name": "b/z", "private": true}]},
        ]}));
        let repos = project_repositories(&SHAPE, &body).unwrap();
        assert_eq!(repos.len(), 2);
        assert_eq!(repos[0]["full_name"], "a/y");
        assert_eq!(repos[0]["default_branch"], "main");
        assert!(repos[1].get("default_branch").is_none());
    }

    #[test]
    fn example_text_allows_tab_lf_cr_only() {
        assert!(example_text(b"a\tb\r\nc").is_some());
        assert!(example_text(b"a\x1bb").is_none());
        assert!(example_text(&[0xff]).is_none());
    }
}
