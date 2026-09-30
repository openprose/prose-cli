//! Not-found errors that name the identifier and a listing
//! command, and Actions built only from the details that are
//! present. Mirrors `cli/bun/src/core/service/not-found.ts`.

use super::render::{argv_text, follow_up_argv};
use crate::error::ErrorCode;
use crate::{OutputMode, RunnerError};
use serde_json::{Map, Value, json};

/// Names the missing resource on a `SERVICE_RESOURCE_NOT_FOUND`:
/// `details.resource` {kind, id}, a reason naming the id,
/// `details.suggestedArgv` for the listing command, and an Action that quotes
/// it. Details a handler already set are kept.
#[must_use]
pub fn explain_not_found(
    error: RunnerError,
    mode: OutputMode,
    kind: &str,
    id: &str,
    list: Option<&[&str]>,
) -> RunnerError {
    explain_not_found_with_hint(error, mode, kind, id, list, None)
}

/// [`explain_not_found`] whose generated reason ends with `hint`.
#[must_use]
pub fn explain_not_found_with_hint(
    mut error: RunnerError,
    mode: OutputMode,
    kind: &str,
    id: &str,
    list: Option<&[&str]>,
    hint: Option<&str>,
) -> RunnerError {
    if error.code != ErrorCode::ServiceResourceNotFound {
        return error;
    }
    let generic = error.action == RunnerError::catalog(ErrorCode::ServiceResourceNotFound).action;
    let details = error.details.get_or_insert_with(|| Box::new(Map::new()));
    if !details.get("resource").is_some_and(Value::is_object) {
        details.insert("resource".to_owned(), json!({"kind": kind, "id": id}));
    }
    let resource_kind = details["resource"]["kind"]
        .as_str()
        .unwrap_or(kind)
        .to_owned();
    let resource_id = details["resource"]["id"].as_str().unwrap_or(id).to_owned();
    if !details.get("reason").is_some_and(Value::is_string) {
        details.insert(
            "reason".to_owned(),
            json!(format!(
                "{resource_kind} {resource_id} was not found{}",
                hint.map(|hint| format!(", {hint}")).unwrap_or_default()
            )),
        );
    }
    if let (false, Some(list)) = (
        details.get("suggestedArgv").is_some_and(Value::is_array),
        list,
    ) {
        details.insert(
            "suggestedArgv".to_owned(),
            json!(follow_up_argv(mode, list)),
        );
    }
    if generic {
        let argv = details
            .get("suggestedArgv")
            .and_then(Value::as_array)
            .map(|argv| {
                argv.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect::<Vec<_>>()
            });
        error.action = match argv {
            Some(argv) => format!(
                "Run `{}` to find the right {resource_kind}, then retry with it.",
                argv_text(&argv)
            ),
            None => format!(
                "Check the {resource_kind} identifier named in Detail, then retry with the right one."
            ),
        };
    }
    error
}

/// Replaces `{ARGUMENT}` placeholders with invocation arguments; `None` when
/// one is missing.
fn fill(template: &str, argument: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    let mut out = String::new();
    let mut rest = template;
    while let Some(start) = rest.find('{') {
        out.push_str(&rest[..start]);
        let end = rest[start..].find('}')? + start;
        out.push_str(&argument(&rest[start + 1..end])?);
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    Some(out)
}

/// Applies the operation's manifest `notFound` entry to a
/// `SERVICE_RESOURCE_NOT_FOUND`.
#[must_use]
pub fn explain_operation_not_found(
    error: RunnerError,
    operation: &Value,
    argument: &dyn Fn(&str) -> Option<String>,
    mode: OutputMode,
) -> RunnerError {
    let base = &operation["notFound"];
    if error.code != ErrorCode::ServiceResourceNotFound || !base.is_object() {
        return error;
    }
    let code = error
        .details
        .as_ref()
        .and_then(|details| details.get("serviceCode"))
        .and_then(Value::as_str);
    let spec = code
        .and_then(|code| base["serviceCodes"].get(code))
        .filter(|spec| spec.is_object())
        .unwrap_or(base);
    let (Some(kind), Some(id)) = (
        spec["resource"].as_str(),
        spec["id"].as_str().and_then(|id| fill(id, argument)),
    ) else {
        return error;
    };
    let list = spec["list"].as_array().and_then(|words| {
        words
            .iter()
            .map(|word| word.as_str().and_then(|word| fill(word, argument)))
            .collect::<Option<Vec<_>>>()
    });
    let words = list
        .as_ref()
        .map(|list| list.iter().map(String::as_str).collect::<Vec<_>>());
    explain_not_found_with_hint(
        error,
        mode,
        kind,
        &id,
        words.as_deref(),
        spec["hint"].as_str(),
    )
}

/// The `SERVICE_REQUEST_REJECTED` Action names only the details that are
/// present (no "whichever of" hedge): the reason, the service message and the
/// service code, in that order; without any, the operation's help.
#[must_use]
pub fn explain_rejected(mut error: RunnerError, mode: OutputMode, help: &[&str]) -> RunnerError {
    if error.code != ErrorCode::ServiceRequestRejected
        || error.action != RunnerError::catalog(ErrorCode::ServiceRequestRejected).action
    {
        return error;
    }
    let details = error.details.get_or_insert_with(|| Box::new(Map::new()));
    let present = ["reason", "serviceMessage", "serviceCode"]
        .iter()
        .filter(|key| details.get(**key).is_some_and(Value::is_string))
        .map(|key| format!("details.{key}"))
        .collect::<Vec<_>>();
    if let [rest @ .., last] = present.as_slice() {
        let names = if rest.is_empty() {
            last.clone()
        } else {
            format!("{} and {last}", rest.join(", "))
        };
        let verb = if rest.is_empty() {
            "describes"
        } else {
            "describe"
        };
        error.action = format!("Correct the request as {names} {verb}, then retry.");
        return error;
    }
    if !details.get("suggestedArgv").is_some_and(Value::is_array) {
        details.insert(
            "suggestedArgv".to_owned(),
            json!(follow_up_argv(mode, help)),
        );
    }
    let argv = details["suggestedArgv"]
        .as_array()
        .map(|argv| {
            argv.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    error.action = format!(
        "The service gave no reason; check the command against `{}`, then retry.",
        argv_text(&argv)
    );
    error
}
