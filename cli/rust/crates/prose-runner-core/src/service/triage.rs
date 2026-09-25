//! `cli service triage`: one read that answers "what state
//! is this session in and what should I run next?".
//!
//! It composes the reads an agent otherwise makes one by one: `GET /health`,
//! `GET /wallet/balance`, `GET /organizations/default`, `GET /runs?limit=5`
//! and `GET /triggers`. Every section carries its own
//! `problem`, so one failing read never hides the others and the operation
//! still exits 0; only cancellation (and invocation errors) end it early.
//! Without a usable credential the account sections are `null` and the
//! `credential` section names the exact variable to set. Only price-side
//! fields are projected (the balance, run prices); nothing cost-side.
//! The result is `shared/schemas/service/discovery.schema.json#/$defs/serviceTriage`;
//! the Bun twin is `cli/bun/src/core/service/triage.ts`. Decisions:
//! docs/service/discovery.md.
use super::{Context, discovery, http, jobs, organizations, render, run_records, wallet};
use crate::RunnerError;
use crate::error::{ErrorCode, human_safe_scalar};
use serde_json::{Map, Value, json};
use std::fmt::Write as _;

/// Run statuses that mean the run is still going (`cli run watch` applies).
/// Any other status, including an unknown one, is treated as finished.
pub const LIVE_RUN_STATUSES: [&str; 6] = [
    "queued",
    "pending",
    "starting",
    "running",
    "input_needed",
    "awaiting_input",
];
/// The low-balance threshold.
pub const DEFAULT_LOW_BALANCE_CENTS: i64 = 500;
/// Recent runs requested and kept (the manifest's `limit`).
const RECENT_RUNS: usize = 5;
/// The run fields `service triage` reports (user fields only).
const TRIAGE_RUN_FIELDS: [&str; 6] = [
    "run_id",
    "status",
    "model",
    "created_at",
    "program_ref",
    "price_cents",
];
/// Jobs kept in the `jobs` section.
const JOBS_SHOWN: usize = 5;
/// `run watch` suggestions for live runs.
const LIVE_WATCH_MAX: usize = 3;
/// `job show` suggestions for jobs reporting an error.
const FAILING_JOBS_MAX: usize = 2;
/// The job fields a triage keeps (a subset of `cli job list`).
const JOB_KEYS: [&str; 9] = [
    "id",
    "type",
    "name",
    "next_fire_at",
    "next_fire_at_iso",
    "last_event_at",
    "last_event_at_iso",
    "last_run_id",
    "last_error",
];

/// The `cli service status` fields a triage keeps.
const HEALTH_KEYS: [&str; 3] = ["status", "default_model", "models"];

/// Indexes into the manifest's `service.triage` requests.
const HEALTH: usize = 0;
const WALLET: usize = 1;
const ORGANIZATION: usize = 2;
const RUNS: usize = 3;
const JOBS: usize = 4;

fn protocol(reason: &str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid).with_detail("reason", reason)
}

fn fetch(
    context: &mut Context<'_>,
    index: usize,
    path: &str,
) -> Result<http::Response, RunnerError> {
    let mut request = http::Request::from_manifest(context.operation, index, path);
    if index == RUNS {
        request = request.query("limit", RECENT_RUNS.to_string());
    }
    context.send(&request)
}

/// A section: the projected fields plus `problem: null`, or `{problem}`.
/// Cancellation is not a section problem: it ends the operation.
fn section(
    context: &Context<'_>,
    projected: Result<Map<String, Value>, RunnerError>,
) -> Result<Value, RunnerError> {
    match projected {
        Ok(mut fields) => {
            fields.insert("problem".into(), Value::Null);
            Ok(Value::Object(fields))
        }
        Err(error) if error.code == ErrorCode::Cancelled => Err(error),
        Err(mut error) => {
            render::redact_error(&mut error, context.known_credential());
            Ok(json!({"problem": error}))
        }
    }
}

/// Whether a failed `/health` means nothing answered at all (a transport
/// failure carries no `serviceStatus`); then the other reads are skipped.
fn unreachable(error: &RunnerError) -> bool {
    error.code == ErrorCode::ServiceUnavailable
        && error
            .details
            .as_ref()
            .is_none_or(|details| !details.contains_key("serviceStatus"))
}

fn detail<'e>(error: &'e RunnerError, key: &str) -> Option<&'e str> {
    error.details.as_ref()?.get(key)?.as_str()
}

/// Executes `cli service triage`.
pub fn execute(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let health = fetch(context, HEALTH, "/health").and_then(|response| {
        let mut health = discovery::health(&response)?;
        health.retain(|key, _| HEALTH_KEYS.contains(&key.as_str()));
        Ok(health)
    });
    let reachable = !matches!(&health, Err(error) if unreachable(error));
    let health = section(context, health)?;

    let variable = context.environment.credential_env;
    let (mut state, source, mut credential_problem) = match context.credential() {
        Ok(_) => (
            "unverified",
            context.credential_source.unwrap_or("environment"),
            None,
        ),
        Err(error) if error.code == ErrorCode::Cancelled => return Err(error),
        Err(mut error) => {
            let state = if error.code == ErrorCode::CredentialStoreUnavailable {
                "unavailable"
            } else {
                match detail(&error, "credentialProblem") {
                    Some("malformed") => "malformed",
                    _ => "missing",
                }
            };
            let source = match detail(&error, "credentialSource") {
                Some("environment") => "environment",
                Some("store") => "store",
                _ => "none",
            };
            render::redact_error(&mut error, context.known_credential());
            (state, source, Some(error))
        }
    };

    let mut account = [Value::Null, Value::Null, Value::Null, Value::Null];
    if reachable && credential_problem.is_none() {
        for (slot, index) in [WALLET, ORGANIZATION, RUNS, JOBS].into_iter().enumerate() {
            let projected = match index {
                WALLET => wallet_section(context),
                ORGANIZATION => organization_section(context),
                RUNS => runs_section(context),
                _ => jobs_section(context),
            };
            match projected {
                Ok(fields) => {
                    state = "valid";
                    account[slot] = section(context, Ok(fields))?;
                }
                Err(error) if error.code == ErrorCode::Cancelled => return Err(error),
                Err(mut error) => {
                    context.annotate_auth(&mut error);
                    if detail(&error, "credentialProblem") == Some("rejected") {
                        render::redact_error(&mut error, context.known_credential());
                        state = "rejected";
                        credential_problem = Some(error);
                        break;
                    }
                    account[slot] = section(context, Err(error))?;
                }
            }
        }
    }
    let [wallet, organization, runs, jobs] = account;
    let credential = json!({
        "variable": variable,
        "source": source,
        "state": state,
        "problem": credential_problem,
    });
    let mut result = json!({
        "health": health,
        "credential": credential,
        "wallet": wallet,
        "organization": organization,
        "runs": runs,
        "jobs": jobs,
    });
    let next = next_commands(context, &result, reachable);
    result["nextCommands"] = Value::Array(next);
    context.human = Some(human(&result));
    Ok(result)
}

fn wallet_section(context: &mut Context<'_>) -> Result<Map<String, Value>, RunnerError> {
    let body = fetch(context, WALLET, "/wallet/balance")?.json_object()?;
    let balance = wallet::project_balance(&body)?;
    let threshold = DEFAULT_LOW_BALANCE_CENTS;
    let available = balance["available_cents"].as_i64().unwrap_or_default();
    let mut fields = Map::new();
    fields.insert("balance".into(), balance);
    fields.insert("low".into(), json!(available < threshold));
    fields.insert("lowBelowCents".into(), json!(threshold));
    Ok(fields)
}

fn organization_section(context: &mut Context<'_>) -> Result<Map<String, Value>, RunnerError> {
    let body = fetch(context, ORGANIZATION, "/organizations/default")?.json_object()?;
    let mut fields = Map::new();
    fields.insert(
        "default".into(),
        organizations::organization(body.get("organization"))?,
    );
    Ok(fields)
}

/// The user fields of a recent run: triage never carries runtime details.
fn triage_run(run: Value) -> Value {
    let Value::Object(fields) = run else {
        return run;
    };
    Value::Object(
        fields
            .into_iter()
            .filter(|(key, _)| TRIAGE_RUN_FIELDS.contains(&key.as_str()))
            .collect(),
    )
}

fn runs_section(context: &mut Context<'_>) -> Result<Map<String, Value>, RunnerError> {
    let body = fetch(context, RUNS, "/runs")?.json_object()?;
    let recent = body
        .get("runs")
        .and_then(Value::as_array)
        .filter(|runs| runs.len() <= 200)
        .ok_or_else(|| protocol("the run list is missing or has more than 200 runs"))?
        .iter()
        .take(RECENT_RUNS)
        .map(|run| run_records::project_run(run, false).map(triage_run))
        .collect::<Result<Vec<_>, _>>()?;
    let live = recent
        .iter()
        .filter(|run| {
            run["status"]
                .as_str()
                .is_some_and(|status| LIVE_RUN_STATUSES.contains(&status))
        })
        .map(|run| run["run_id"].clone())
        .collect::<Vec<_>>();
    let mut fields = Map::new();
    fields.insert("recent".into(), Value::Array(recent));
    fields.insert("live".into(), Value::Array(live));
    Ok(fields)
}

fn jobs_section(context: &mut Context<'_>) -> Result<Map<String, Value>, RunnerError> {
    let body = fetch(context, JOBS, "/triggers")?.json_object()?;
    let (all, max) = jobs::project_jobs(&body)?;
    let shown = all
        .iter()
        .take(JOBS_SHOWN)
        .map(|entry| {
            let mut job = Map::new();
            for key in JOB_KEYS {
                if let Some(value) = entry.get(key).filter(|value| !value.is_null()) {
                    job.insert(key.into(), value.clone());
                }
            }
            Value::Object(job)
        })
        .collect::<Vec<_>>();
    let mut fields = Map::new();
    fields.insert("total".into(), json!(all.len()));
    fields.insert("max".into(), max);
    fields.insert("jobs".into(), Value::Array(shown));
    Ok(fields)
}

fn reason_of(problem: &Value) -> String {
    problem["details"]["reason"]
        .as_str()
        .or_else(|| problem["message"].as_str())
        .unwrap_or_default()
        .to_owned()
}

fn cents_text(cents: i64) -> String {
    format!("{}.{:02}", cents / 100, (cents % 100).abs())
}

/// `nextCommands`, derived from the sections in a fixed order: the
/// credential variable, an unreachable service, live runs, a low balance,
/// jobs reporting an error. Commands keep the output mode.
fn next_commands(context: &Context<'_>, result: &Value, reachable: bool) -> Vec<Value> {
    let mut next = Vec::new();
    let credential = &result["credential"];
    if !credential["problem"].is_null() {
        next.push(json!({
            "why": reason_of(&credential["problem"]),
            "argv": null,
            "env": credential["variable"],
        }));
    }
    if !reachable {
        next.push(json!({
            "why": "the service did not respond; check your network connection, then retry",
            "argv": context.follow_up_argv(&["service", "status"]),
            "env": null,
        }));
    }
    if let Some(live) = result["runs"]["live"].as_array() {
        let recent = result["runs"]["recent"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        for run_id in live.iter().filter_map(Value::as_str).take(LIVE_WATCH_MAX) {
            let status = recent
                .iter()
                .find(|run| run["run_id"] == run_id)
                .and_then(|run| run["status"].as_str())
                .unwrap_or_default();
            next.push(json!({
                "why": format!("run {run_id} is {status}"),
                "argv": context.follow_up_argv(&["run", "watch", run_id]),
                "env": null,
            }));
        }
    }
    if result["wallet"]["low"] == true {
        let threshold = result["wallet"]["lowBelowCents"]
            .as_i64()
            .unwrap_or_default();
        let amount = threshold.to_string();
        next.push(json!({
            "why": format!(
                "the available balance ${} is below ${}",
                result["wallet"]["balance"]["available_dollars"].as_str().unwrap_or_default(),
                cents_text(threshold)
            ),
            "argv": context.follow_up_argv(&["wallet", "topup", "--amount-cents", &amount, "--preview"]),
            "env": null,
        }));
    }
    if let Some(shown) = result["jobs"]["jobs"].as_array() {
        for job in shown
            .iter()
            .filter(|job| job.get("last_error").is_some())
            .take(FAILING_JOBS_MAX)
        {
            let id = job["id"].as_str().unwrap_or_default();
            next.push(json!({
                "why": format!("job {id} reported an error"),
                "argv": context.follow_up_argv(&["job", "show", id]),
                "env": null,
            }));
        }
    }
    next
}

fn problem_line(label: &str, problem: &Value) -> String {
    format!(
        "{label}: {}: {}\n",
        problem["code"].as_str().unwrap_or_default(),
        human_safe_scalar(&reason_of(problem))
    )
}

fn text(value: &Value) -> String {
    human_safe_scalar(value.as_str().unwrap_or_default())
}

/// The human report: at most 24 lines (one per section, up to five runs,
/// five jobs and seven next commands).
fn human(result: &Value) -> String {
    let mut out = String::new();
    let health = &result["health"];
    if health["problem"].is_null() {
        let _ = writeln!(
            out,
            "Service: {}, default model {}",
            text(&health["status"]),
            text(&health["default_model"])
        );
    } else {
        out.push_str(&problem_line("Service", &health["problem"]));
    }
    let credential = &result["credential"];
    let variable = text(&credential["variable"]);
    let origin = match credential["source"].as_str() {
        Some("none") => format!("{variable} not set"),
        Some("environment") => format!("from {variable}"),
        Some("store") => "from the credential store".to_owned(),
        Some(source) => format!("{variable} from {source}"),
        None => variable,
    };
    let _ = write!(out, "Credential: {}, {origin}", text(&credential["state"]));
    if credential["problem"].is_null() {
        out.push('\n');
    } else {
        let _ = writeln!(
            out,
            "; {}",
            human_safe_scalar(&reason_of(&credential["problem"]))
        );
    }
    let wallet = &result["wallet"];
    if wallet.is_null() {
        out.push_str("Wallet: skipped\n");
    } else if wallet["problem"].is_null() {
        let balance = &wallet["balance"];
        let _ = write!(
            out,
            "Wallet: ${} available, ${} reserved",
            text(&balance["available_dollars"]),
            text(&balance["reserved_dollars"])
        );
        if wallet["low"] == true {
            let _ = write!(
                out,
                " (low: below ${})",
                cents_text(wallet["lowBelowCents"].as_i64().unwrap_or_default())
            );
        }
        out.push('\n');
    } else {
        out.push_str(&problem_line("Wallet", &wallet["problem"]));
    }
    let organization = &result["organization"];
    if organization.is_null() {
        out.push_str("Organization: skipped\n");
    } else if organization["problem"].is_null() {
        let default = &organization["default"];
        let _ = write!(out, "Organization: {}", text(&default["slug"]));
        if let Some(role) = default["role"].as_str() {
            let _ = write!(out, " ({})", human_safe_scalar(role));
        }
        out.push('\n');
    } else {
        out.push_str(&problem_line("Organization", &organization["problem"]));
    }
    let runs = &result["runs"];
    if runs.is_null() {
        out.push_str("Runs: skipped\n");
    } else if runs["problem"].is_null() {
        let recent = runs["recent"].as_array().cloned().unwrap_or_default();
        let live = runs["live"].as_array().map_or(0, Vec::len);
        let _ = writeln!(out, "Runs: {} recent, {live} live", recent.len());
        for run in &recent {
            let _ = writeln!(
                out,
                "  {}  {}  {}  {}",
                text(&run["run_id"]),
                text(&run["status"]),
                text(&run["model"]),
                text(&run["created_at"])
            );
        }
    } else {
        out.push_str(&problem_line("Runs", &runs["problem"]));
    }
    let jobs = &result["jobs"];
    if jobs.is_null() {
        out.push_str("Jobs: skipped\n");
    } else if jobs["problem"].is_null() {
        let total = jobs["total"].as_u64().unwrap_or_default();
        match jobs["max"].as_i64() {
            Some(max) => {
                let _ = writeln!(out, "Jobs: {total} of {max} allowed");
            }
            None => {
                let _ = writeln!(out, "Jobs: {total}");
            }
        }
        for job in jobs["jobs"].as_array().cloned().unwrap_or_default() {
            let name = job.get("name").map_or_else(|| "-".to_owned(), text);
            let _ = write!(
                out,
                "  {}  {}  {name}",
                text(&job["id"]),
                text(&job["type"])
            );
            if job.get("last_error").is_some() {
                out.push_str("  (error)");
            }
            out.push('\n');
        }
    } else {
        out.push_str(&problem_line("Jobs", &jobs["problem"]));
    }
    let next = result["nextCommands"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    if next.is_empty() {
        out.push_str("Next: nothing needs attention\n");
    } else {
        out.push_str("Next:\n");
        for command in &next {
            let action = match command["argv"].as_array() {
                Some(argv) => render::argv_text(
                    &argv
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect::<Vec<_>>(),
                ),
                None => format!("set {}", command["env"].as_str().unwrap_or_default()),
            };
            let _ = writeln!(
                out,
                "  {}  # {}",
                human_safe_scalar(&action),
                human_safe_scalar(command["why"].as_str().unwrap_or_default())
            );
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cents_render_as_dollars() {
        assert_eq!(cents_text(500), "5.00");
        assert_eq!(cents_text(35), "0.35");
        assert_eq!(cents_text(12345), "123.45");
    }

    #[test]
    fn human_report_stays_within_25_lines_at_its_largest() {
        let run = json!({"run_id": "run_1", "status": "running", "model": "m", "created_at": "t"});
        let job = json!({"id": "j", "type": "schedule", "last_error": "x"});
        let command = json!({"why": "w", "argv": ["cli", "run", "watch", "run_1"], "env": null});
        let result = json!({
            "health": {"problem": null, "status": "ok", "default_model": "m", "models": ["m"]},
            "credential": {"variable": "V", "source": "environment", "state": "valid", "problem": null},
            "wallet": {"problem": null, "balance": {"available_dollars": "1.00", "reserved_dollars": "0.00"},
                       "low": true, "lowBelowCents": 500},
            "organization": {"problem": null, "default": {"slug": "o", "role": "admin"}},
            "runs": {"problem": null, "recent": vec![run; RECENT_RUNS], "live": ["run_1"]},
            "jobs": {"problem": null, "total": 9, "max": 10, "jobs": vec![job; JOBS_SHOWN]},
            "nextCommands": vec![command; LIVE_WATCH_MAX + 1 + FAILING_JOBS_MAX],
        });
        let text = human(&result);
        assert!(text.lines().count() <= 24);
        assert!(text.starts_with("Service: ok, default model m\n"), "{text}");
    }
}
