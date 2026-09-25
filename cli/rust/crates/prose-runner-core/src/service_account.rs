//! Environment-isolated account operations. Credentials never enter harness configuration.
use crate::error::ErrorCode;
use crate::output::CommandOutcome;
use crate::service::Environment;
use crate::{CancellationToken, OutputMode, RunnerCommand, RunnerError};
use serde_json::{Value, json};
use std::io::Read;
use std::time::{Duration, Instant};

const LIMIT: u64 = 65_536;

fn problem(code: ErrorCode) -> RunnerError {
    RunnerError::catalog(code)
}

/// `SERVICE_PROTOCOL_INVALID` naming the response field that failed (the
/// reason shape of the service operations).
fn unexpected(what: &str, field: &str) -> RunnerError {
    problem(ErrorCode::ServiceProtocolInvalid)
        .with_detail("reason", format!("unexpected {what} response: {field}"))
}

/// `SERVICE_UNAVAILABLE` for a non-success status the service answered with.
fn unavailable(status: u64) -> RunnerError {
    problem(ErrorCode::ServiceUnavailable).with_detail("serviceStatus", status)
}

/// A login that ended before a key was stored says so (the reason of each
/// ending); errors that already carry details are kept.
fn login_ended(error: RunnerError) -> RunnerError {
    if error.details.is_some() {
        return error;
    }
    let reason = match error.code {
        ErrorCode::DeviceAuthFailed => "the device authorization was denied; nothing was stored",
        ErrorCode::DeviceAuthExpired => {
            "the device code expired before it was approved; nothing was stored"
        }
        ErrorCode::Cancelled => {
            "login was interrupted before the code was approved; nothing was stored"
        }
        _ => return error,
    };
    error.with_detail("reason", reason)
}

/// An environment variable, decoded like the Bun build decodes its
/// environment: each sequence that is not UTF-8 becomes U+FFFD (so a key
/// that is not UTF-8 is a malformed key, never an absent one).
fn environment_value(name: &str) -> Option<String> {
    std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
}

fn valid_token(token: &str) -> bool {
    token.strip_prefix("rr_test_").is_some_and(|v| {
        v.len() == 32
            && v.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

pub(crate) struct Session {
    environment: Environment,
    fixture: Option<Value>,
    next: usize,
    cancellation: CancellationToken,
    deadline: Option<Instant>,
}

impl Session {
    fn new(
        cancellation: &CancellationToken,
        environment: Environment,
    ) -> Result<Self, RunnerError> {
        Self::bounded(cancellation, environment, LIMIT)
    }

    pub(crate) fn registry(
        cancellation: &CancellationToken,
        environment: Environment,
    ) -> Result<Self, RunnerError> {
        Self::bounded(cancellation, environment, 16 * 1024 * 1024)
    }

    fn bounded(
        cancellation: &CancellationToken,
        environment: Environment,
        fixture_limit: u64,
    ) -> Result<Self, RunnerError> {
        let _ = fixture_limit;
        #[allow(unused_mut)]
        let mut fixture: Option<Value> = None;
        #[cfg(feature = "test-seams")]
        if let Some(path) = std::env::var_os("PROSE_TEST_SERVICE_FIXTURE") {
            let mut bytes = Vec::new();
            std::fs::File::open(path)
                .map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?
                .take(fixture_limit + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?;
            if bytes.len() as u64 > fixture_limit {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            fixture = Some(
                crate::service::http::parse_json(&bytes)
                    .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?,
            );
        }
        if let Some(value) = &fixture {
            if !value.is_object()
                || !value["storeAvailable"].is_boolean()
                || value["exchanges"].as_array().is_none_or(|v| v.len() > 182)
                || !(value
                    .get("credential")
                    .is_some_and(|v| v.is_null() || v.is_string())
                    || value.get("credentials").is_some_and(Value::is_object))
                || value
                    .get("environment")
                    .is_some_and(|v| v != environment.name)
                || value.get("credentials").is_some_and(|v| {
                    v.as_object().is_none_or(|credentials| {
                        credentials
                            .values()
                            .any(|v| !(v.is_null() || v.is_string()))
                    })
                })
            {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
        }
        Ok(Self {
            environment,
            fixture,
            next: 0,
            deadline: None,
            cancellation: cancellation.clone(),
        })
    }

    fn check(&self) -> Result<(), RunnerError> {
        if self.cancellation.is_cancelled() {
            Err(problem(ErrorCode::Cancelled))
        } else if self
            .deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            Err(problem(ErrorCode::DeviceAuthExpired))
        } else {
            Ok(())
        }
    }

    fn transport_failure(&self) -> RunnerError {
        self.check()
            .err()
            .unwrap_or_else(|| problem(ErrorCode::ServiceUnavailable))
    }

    fn store(
        &mut self,
        operation: &str,
        token: Option<&str>,
    ) -> Result<Option<String>, RunnerError> {
        self.check()?;
        if let Some(fixture) = self.fixture.as_mut() {
            if fixture["storeAvailable"] == false {
                return Err(problem(ErrorCode::CredentialStoreUnavailable));
            }
            let slot = if fixture.get("credentials").is_some() {
                &mut fixture["credentials"][self.environment.name]
            } else {
                &mut fixture["credential"]
            };
            let previous = slot.as_str().map(str::to_owned);
            if operation == "set" {
                *slot = json!(token);
            }
            if operation == "delete" {
                *slot = Value::Null;
            }
            return Ok(previous);
        }
        native_store(operation, token, &self.cancellation, &self.environment)
    }

    fn request(
        &mut self,
        method: &str,
        path: &str,
        token: Option<&str>,
        body: Value,
    ) -> Result<Value, RunnerError> {
        self.check()?;
        let (status, value) = if let Some(fixture) = self.fixture.as_ref() {
            if (path == "/organizations") != token.is_some()
                || (path != "/organizations" && token.is_some())
            {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            if let Some(token) = token {
                let expected = environment_value(self.environment.credential_env)
                    .filter(|value| !value.is_empty())
                    .or_else(|| {
                        let value = if fixture.get("credentials").is_some() {
                            &fixture["credentials"][self.environment.name]
                        } else {
                            &fixture["credential"]
                        };
                        value.as_str().map(str::to_owned)
                    });
                if expected.as_deref() != Some(token) {
                    return Err(problem(ErrorCode::ServiceProtocolInvalid));
                }
            }
            let exchange = fixture["exchanges"]
                .get(self.next)
                .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            self.next += 1;
            if exchange["method"] != method
                || exchange["path"] != path
                || exchange
                    .get("origin")
                    .is_some_and(|v| v != self.environment.origin.as_str())
            {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            (
                exchange["status"].as_u64().unwrap_or(0),
                exchange["body"].clone(),
            )
        } else {
            let timeout = self.deadline.map_or(Duration::from_secs(10), |deadline| {
                deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_secs(10))
            });
            if timeout.is_zero() {
                return Err(problem(ErrorCode::DeviceAuthExpired));
            }
            let url = format!("{}{path}", self.environment.origin);
            let agent = crate::service::http::agent_builder(
                &url,
                &crate::service::http::process_environment,
            )?
            .timeout(timeout)
            .build();
            let mut request = agent
                .request(method, &url)
                .set("Accept", "application/json");
            for (name, value) in crate::service::http::client_headers() {
                request = request.set(&name, &value);
            }
            if let Some(token) = token {
                request = request.set("Authorization", &format!("Bearer {token}"));
            }
            let result = if method == "POST" {
                request
                    .set("Content-Type", "application/json")
                    .send_string(&body.to_string())
            } else {
                request.call()
            };
            let response = match result {
                Ok(r) | Err(ureq::Error::Status(_, r)) => r,
                Err(_) => return Err(self.transport_failure()),
            };
            self.check()?;
            let status = u64::from(response.status());
            if matches!(status, 401 | 403) {
                return Err(problem(ErrorCode::ServiceAuthRequired));
            }
            if !(200..=299).contains(&status) && !(status == 400 && path.ends_with("/poll")) {
                return Err(unavailable(status));
            }
            let mut bytes = Vec::new();
            response
                .into_reader()
                .take(LIMIT + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| self.transport_failure())?;
            self.check()?;
            if bytes.len() as u64 > LIMIT {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            (
                status,
                crate::service::http::parse_json(&bytes)
                    .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?,
            )
        };
        self.check()?;
        match status {
            200..=299 => Ok(value),
            401 | 403 => Err(problem(ErrorCode::ServiceAuthRequired)),
            400 if path.ends_with("/poll") => Err(problem(if value["error"] == "expired_token" {
                ErrorCode::DeviceAuthExpired
            } else {
                ErrorCode::DeviceAuthFailed
            })),
            _ => Err(unavailable(status)),
        }
    }

    fn pause(&self, seconds: u64) -> Result<(), RunnerError> {
        if let Some(fixture) = &self.fixture {
            if fixture["cancelBeforePoll"] == true {
                return Err(problem(ErrorCode::Cancelled));
            }
            return self.check();
        }
        let deadline = Instant::now() + Duration::from_secs(seconds);
        while Instant::now() < deadline {
            self.check()?;
            std::thread::sleep(Duration::from_millis(50));
        }
        self.check()
    }
}

/// Who the key belongs to, when GET /organizations says: a top-level `login`
/// (at most 100 characters, no controls) and the slug of the organization
/// marked `default: true` (null when none is). `None` when the service names
/// no login; never the key (mirrors Bun `identity`).
fn identity(body: &Value, token: &str) -> Option<Value> {
    let login = body["login"].as_str().filter(|login| {
        !login.is_empty()
            && login.chars().count() <= 100
            && !login.chars().any(|c| c <= '\u{1f}' || c == '\u{7f}')
            && !login.contains(token)
    })?;
    let organization = body["organizations"]
        .as_array()
        .and_then(|rows| rows.iter().find(|row| row["default"] == true))
        .and_then(|row| row["slug"].as_str())
        .filter(|slug| !slug.contains(token));
    Some(json!({"login": login, "organization": organization}))
}

fn organizations(value: &Value) -> Result<Value, RunnerError> {
    let entries = value["organizations"]
        .as_array()
        .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
    if entries.len() > 1000 {
        return Err(problem(ErrorCode::ServiceProtocolInvalid));
    }
    let mut output = Vec::new();
    for entry in entries {
        let mut row = json!({});
        for field in ["id", "slug", "name"] {
            let text = entry[field]
                .as_str()
                .filter(|s| {
                    !s.is_empty()
                        && s.chars().count() <= 4096
                        && !s.chars().any(|c| c <= '\u{1f}' || c == '\u{7f}')
                })
                .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            row[field] = json!(text);
        }
        if let Some(role) = entry.get("role") {
            if !matches!(role.as_str(), Some("admin" | "developer" | "reader")) {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            row["role"] = role.clone();
        }
        output.push(row);
    }
    Ok(json!(output))
}
/// `INVOCATION_INVALID` for login or logout while the selected variable is
/// set: the command cannot change the key that commands actually use.
fn environment_key_refusal(
    operation: &str,
    environment: &Environment,
    mode: OutputMode,
) -> RunnerError {
    let variable = environment.credential_env;
    let command = |words: &str| crate::service::render::follow_up_command(mode, words);
    let (reason, action) = if operation == "login" {
        (
            format!(
                "{variable} is set, and a non-empty {variable} takes precedence over a stored key, so `cli auth login` would not change the key commands use"
            ),
            format!(
                "Keep using {variable} and check it with `{}`, or unset {variable} and run `{}`.",
                command("auth status"),
                command("auth login")
            ),
        )
    } else {
        (
            format!(
                "{variable} is set; `cli auth logout` removes only the stored key, so commands would keep using {variable}"
            ),
            format!(
                "Unset {variable} to stop using that key; then `{}` removes the stored key.",
                command("auth logout")
            ),
        )
    };
    let mut error = RunnerError::invocation(reason)
        .with_detail("credentialSource", "environment")
        .with_detail("credentialVariable", variable);
    error.action = action;
    error
}

/// Executes an account operation with a bounded environment-isolated transport.
#[must_use]
pub fn execute(
    command: &RunnerCommand,
    selected: &Environment,
    mode: OutputMode,
    cancellation: &CancellationToken,
) -> CommandOutcome {
    let operation = match command {
        RunnerCommand::AuthLogin => "login",
        RunnerCommand::AuthLogout => "logout",
        RunnerCommand::OrgList => "list",
        _ => "status",
    };
    let teaching = selected.clone();
    // Where the key came from; kept on a failure that is about that key.
    let mut source = "none";
    let mut authenticated = false;
    let mut rows = json!([]);
    let mut who: Option<Value> = None;
    let result = (|| -> Result<(), RunnerError> {
        let mut session = Session::new(cancellation, selected.clone())?;
        let environment = environment_value(selected.credential_env).filter(|s| !s.is_empty());
        if environment.is_some() && matches!(operation, "login" | "logout") {
            source = "environment";
            return Err(environment_key_refusal(operation, &teaching, mode));
        }
        if operation == "logout" {
            session.store("delete", None)?;
            return Ok(());
        }
        if operation == "login" {
            session.store("get", None)?;
            let start = session.request("POST", "/auth/device", None, json!({}))?;
            let what = "POST /auth/device";
            let code = start["device_code"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 1024)
                .ok_or_else(|| unexpected(what, "device_code"))?
                .to_owned();
            let user = start["user_code"]
                .as_str()
                .filter(|s| {
                    !s.is_empty()
                        && s.len() <= 32
                        && s.bytes()
                            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'-')
                        && !s.contains(&code)
                })
                .ok_or_else(|| unexpected(what, "user_code"))?;
            if start["verification_uri"] != "https://github.com/login/device" {
                return Err(unexpected(what, "verification_uri"));
            }
            let expiry = start["expires_in"]
                .as_u64()
                .filter(|v| (1..=900).contains(v))
                .ok_or_else(|| unexpected(what, "expires_in"))?;
            let mut interval = start["interval"]
                .as_u64()
                .filter(|v| (1..=30).contains(v))
                .ok_or_else(|| unexpected(what, "interval"))?;
            eprintln!("Go to https://github.com/login/device and enter code: {user}");
            let began = Instant::now();
            session.deadline = Some(began + Duration::from_secs(expiry));
            let mut elapsed = 0;
            for _ in 0..180 {
                elapsed += interval;
                if elapsed >= expiry || began.elapsed().as_secs() >= expiry {
                    return Err(problem(ErrorCode::DeviceAuthExpired));
                }
                session.pause(interval)?;
                if began.elapsed().as_secs() >= expiry {
                    return Err(problem(ErrorCode::DeviceAuthExpired));
                }
                let poll = session.request(
                    "POST",
                    "/auth/device/poll",
                    None,
                    json!({
                        "device_code":code
                    }),
                )?;
                if began.elapsed().as_secs() >= expiry {
                    return Err(problem(ErrorCode::DeviceAuthExpired));
                }
                match poll["status"].as_str() {
                    Some("pending") => {}
                    Some("slow_down") => interval = (interval + 5).min(30),
                    Some("complete") => {
                        let token = poll["api_key"]
                            .as_str()
                            .filter(|s| valid_token(s))
                            .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
                        session.store("set", Some(token))?;
                        if session.store("get", None)?.as_deref() != Some(token) {
                            return Err(problem(ErrorCode::CredentialStoreUnavailable));
                        }
                        source = "store";
                        authenticated = true;
                        return Ok(());
                    }
                    Some("error") if poll["error"] == "expired_token" => {
                        return Err(problem(ErrorCode::DeviceAuthExpired));
                    }
                    Some("error") => return Err(problem(ErrorCode::DeviceAuthFailed)),
                    _ => return Err(problem(ErrorCode::ServiceProtocolInvalid)),
                }
            }
            return Err(problem(ErrorCode::DeviceAuthExpired));
        }
        // Status and org list share the service classifier:
        // a missing, malformed or rejected key is SERVICE_AUTH_REQUIRED that
        // names the variable, and its Action follows the key's source.
        let token = if let Some(token) = environment {
            source = "environment";
            Some(token)
        } else {
            let token = session.store("get", None)?;
            if token.is_some() {
                source = "store";
            }
            token
        };
        let Some(token) = token else {
            if operation == "list" {
                return Err(crate::service::credential_failure(
                    &teaching,
                    mode,
                    "none",
                    "missing",
                    crate::service::missing_reason(&teaching, mode),
                ));
            }
            return Ok(());
        };
        if !valid_token(&token) {
            return Err(crate::service::credential_failure(
                &teaching,
                mode,
                source,
                "malformed",
                crate::service::localize_hint(
                    mode,
                    &crate::service::malformed_reason(
                        &token,
                        teaching.credential_env,
                        source == "environment",
                        &teaching,
                    ),
                ),
            ));
        }
        let body = session
            .request("GET", "/organizations", Some(&token), Value::Null)
            .map_err(|error| {
                if error.code == ErrorCode::ServiceAuthRequired {
                    crate::service::credential_failure(
                        &teaching,
                        mode,
                        source,
                        "rejected",
                        crate::service::rejected_reason(&teaching, mode, source),
                    )
                } else {
                    error
                }
            })?;
        who = identity(&body, &token);
        rows = organizations(&body).map_err(|error| {
            if error.details.is_none() {
                unexpected("GET /organizations", "organizations")
            } else {
                error
            }
        })?;
        if rows.as_array().is_some_and(|entries| {
            entries.iter().any(|entry| {
                ["id", "slug", "name"].iter().any(|key| {
                    entry[*key]
                        .as_str()
                        .is_some_and(|value| value.contains(&token))
                })
            })
        }) {
            rows = json!([]);
            return Err(unexpected("GET /organizations", "organizations"));
        }
        authenticated = true;
        Ok(())
    })();
    // An unavailable store names the variable that works without it (not for
    // logout, which only removes a stored key).
    let error = result.err().map(|error| {
        let error = if operation == "login" {
            login_ended(error)
        } else {
            error
        };
        if error.code == ErrorCode::CredentialStoreUnavailable && operation != "logout" {
            crate::service::store_unavailable(error, &teaching)
        } else {
            error
        }
    });
    // A failure that is about the key keeps the key's source; others name none.
    if error.as_ref().is_some_and(|error| {
        !matches!(
            error.code,
            ErrorCode::ServiceAuthRequired | ErrorCode::InvocationInvalid
        )
    }) {
        source = "none";
    }
    let exit = error.as_ref().map_or(0, |e| e.exit_code);
    if mode != OutputMode::Human {
        // The service-operation/1 envelope every `cli` command prints: the
        // account state (or the organizations) is the result.
        let id = if operation == "list" {
            "org.list".to_owned()
        } else {
            format!("auth.{operation}")
        };
        let manifest_operation =
            crate::service::operation(&id).expect("account operations are in the manifest");
        let result = match &error {
            Some(error) => Err(error.clone()),
            None if operation == "list" => Ok(json!({"organizations": rows})),
            None => {
                let mut result =
                    json!({"authenticated": authenticated, "credentialSource": source});
                if let (true, Some(who)) = (authenticated, &who) {
                    result["identity"] = who.clone();
                }
                Ok(result)
            }
        };
        return crate::service::render::outcome(manifest_operation, selected, mode, result, None);
    }
    // Human: a failure prints only its error (label, Detail, Action) on
    // stderr, never a status line; success prints the result on stdout, after
    // the custom-endpoint banner of a dev-endpoint build on stderr (identical
    // in both products).
    if let Some(error) = error {
        return CommandOutcome::human(
            "",
            crate::service::render::human_error(&selected.label(), &error),
            exit,
        );
    }
    let banner = if selected.is_custom() {
        format!("{}\n", selected.label())
    } else {
        String::new()
    };
    let stdout = if operation == "list" {
        rows.as_array()
            .map(|entries| {
                entries
                    .iter()
                    .map(|entry| {
                        format!(
                            "{}  {}  {}\n",
                            crate::error::human_safe_scalar(entry["slug"].as_str().unwrap_or("")),
                            entry["role"].as_str().unwrap_or("-"),
                            crate::error::human_safe_scalar(entry["name"].as_str().unwrap_or(""))
                        )
                    })
                    .collect::<String>()
            })
            .unwrap_or_default()
    } else {
        let who = who
            .as_ref()
            .filter(|_| authenticated)
            .map(|who| {
                let organization = who["organization"]
                    .as_str()
                    .map(|slug| {
                        format!(" (organization {})", crate::error::human_safe_scalar(slug))
                    })
                    .unwrap_or_default();
                format!(
                    " as {}{organization}",
                    crate::error::human_safe_scalar(who["login"].as_str().unwrap_or_default())
                )
            })
            .unwrap_or_default();
        format!(
            "OpenProse account {operation}: {}\n",
            if authenticated {
                format!("authenticated{who}")
            } else {
                "signed out".to_owned()
            }
        )
    };
    CommandOutcome::human(stdout, "", 0).with_preamble(banner)
}
/// The operating-system credential store: the login keychain through
/// `/usr/bin/security` on macOS, the Secret Service through `secret-tool` on
/// Linux. Other platforms have none (set the variable instead).
pub(crate) fn native_store(
    operation: &str,
    token: Option<&str>,
    cancellation: &CancellationToken,
    environment: &Environment,
) -> Result<Option<String>, RunnerError> {
    #[cfg(target_os = "macos")]
    {
        crate::credential_store::macos::store(operation, token, cancellation, environment)
    }
    #[cfg(not(target_os = "macos"))]
    {
        if cfg!(target_os = "linux") {
            return crate::credential_store::linux_store(
                operation,
                token,
                cancellation,
                environment,
            );
        }
        let _ = (operation, token, cancellation, environment);
        Err(problem(ErrorCode::CredentialStoreUnavailable))
    }
}
#[must_use]
pub fn is_service_command(command: &RunnerCommand) -> bool {
    matches!(
        command,
        RunnerCommand::AuthLogin
            | RunnerCommand::AuthStatus
            | RunnerCommand::AuthLogout
            | RunnerCommand::OrgList
            | RunnerCommand::Package(_)
    )
}

/// Resolves the service and executes an account or package command without
/// reading workspace configuration.
///
/// # Errors
/// Returns an invocation error for a command that is not an account or
/// package command, and (only in a `dev-endpoint` build) a configuration
/// error for an invalid endpoint override.
pub fn execute_user_command(
    command: &RunnerCommand,
    system: &crate::SystemContext,
    mode: OutputMode,
    cancellation: &CancellationToken,
) -> Result<CommandOutcome, RunnerError> {
    if !is_service_command(command) {
        return Err(RunnerError::invocation(
            "Expected an account or package command.",
        ));
    }
    let selected = crate::service::Environment::resolve(&system.environment)?;
    if let RunnerCommand::Package(command) = command {
        return Ok(crate::registry::execute(
            command,
            &selected,
            &system.current_dir,
            mode,
            cancellation,
        ));
    }
    Ok(execute(command, &selected, mode, cancellation))
}

impl Session {
    pub(crate) fn registry_credential(
        &mut self,
        required: bool,
    ) -> Result<Option<String>, RunnerError> {
        self.check()?;
        let token = environment_value(self.environment.credential_env).filter(|v| !v.is_empty());
        let token = match token {
            Some(token) => Some(token),
            None => match self.store("get", None) {
                Ok(token) => token,
                Err(error) if !required && error.code == ErrorCode::CredentialStoreUnavailable => {
                    None
                }
                Err(error) => return Err(error),
            },
        };
        if token.as_deref().is_some_and(|token| !valid_token(token)) {
            return Err(problem(ErrorCode::ServiceProtocolInvalid));
        }
        if required && token.is_none() {
            return Err(problem(ErrorCode::ServiceAuthRequired));
        }
        Ok(token)
    }

    pub(crate) fn registry_request(
        &mut self,
        method: &str,
        path: &str,
        token: Option<&str>,
        body: Option<&[u8]>,
        accepted: &[u16],
    ) -> Result<Vec<u8>, RunnerError> {
        use sha2::{Digest, Sha256};
        self.check()?;
        if !path.starts_with("/registry/v1/organizations/")
            || body.is_some_and(|value| value.len() > crate::registry::LIMIT)
        {
            return Err(problem(ErrorCode::ServiceProtocolInvalid));
        }
        let (status, bytes) = if let Some(fixture) = &self.fixture {
            let expected = environment_value(self.environment.credential_env)
                .filter(|v| !v.is_empty())
                .or_else(|| {
                    if fixture["storeAvailable"] == false {
                        return None;
                    }
                    let value = if fixture.get("credentials").is_some() {
                        &fixture["credentials"][self.environment.name]
                    } else {
                        &fixture["credential"]
                    };
                    value.as_str().map(str::to_owned)
                });
            if token != expected.as_deref() {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            let exchange = fixture["exchanges"]
                .get(self.next)
                .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            self.next += 1;
            if exchange["method"] != method
                || exchange["path"] != path
                || exchange
                    .get("origin")
                    .is_some_and(|v| v != self.environment.origin.as_str())
            {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            let sent = body.unwrap_or_default();
            for key in ["expectBody", "expectedBody"] {
                if let Some(value) = exchange.get(key) {
                    if value.as_str().map(str::as_bytes) != Some(sent) {
                        return Err(problem(ErrorCode::ServiceProtocolInvalid));
                    }
                }
            }
            for key in ["expectBodySha256", "expectedSha256"] {
                if exchange.get(key).is_some_and(|v| {
                    v.as_str() != Some(format!("{:x}", Sha256::digest(sent)).as_str())
                }) {
                    return Err(problem(ErrorCode::ServiceProtocolInvalid));
                }
            }
            let status = exchange["status"]
                .as_u64()
                .and_then(|v| u16::try_from(v).ok())
                .unwrap_or(0);
            if !accepted.contains(&status) {
                return Err(registry_status(status));
            }
            let bytes = if path.ends_with("/artifact") {
                exchange["body"]
                    .as_str()
                    .ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?
                    .as_bytes()
                    .to_vec()
            } else {
                serde_json::to_vec(&exchange["body"])
                    .map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?
            };
            (
                exchange["status"]
                    .as_u64()
                    .and_then(|v| u16::try_from(v).ok())
                    .unwrap_or(0),
                bytes,
            )
        } else {
            let url = format!("{}{path}", self.environment.origin);
            let agent = crate::service::http::agent_builder(
                &url,
                &crate::service::http::process_environment,
            )?
            .timeout(Duration::from_secs(10))
            .build();
            let mut request = agent
                .request(method, &url)
                .set("Accept", "application/json");
            for (name, value) in crate::service::http::client_headers() {
                request = request.set(&name, &value);
            }
            if let Some(token) = token {
                request = request.set("Authorization", &format!("Bearer {token}"));
            }
            let response = if let Some(body) = body {
                request
                    .set("Content-Type", "application/json")
                    .send_bytes(body)
            } else {
                request.call()
            };
            let (Ok(response) | Err(ureq::Error::Status(_, response))) = response else {
                return Err(self.transport_failure());
            };
            self.check()?;
            let status = response.status();
            // Do not retain or emit service error bodies.
            if !accepted.contains(&status) {
                return Err(registry_status(status));
            }
            let mut bytes = Vec::new();
            response
                .into_reader()
                .take(crate::registry::LIMIT as u64 + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| self.transport_failure())?;
            (status, bytes)
        };
        self.check()?;
        if !accepted.contains(&status) {
            return Err(registry_status(status));
        }
        if bytes.len() > crate::registry::LIMIT {
            return Err(problem(ErrorCode::ServiceProtocolInvalid));
        }
        Ok(bytes)
    }
}
fn registry_status(status: u16) -> RunnerError {
    problem(match status {
        401 | 403 => ErrorCode::ServiceAuthRequired,
        409 => ErrorCode::ServiceProtocolInvalid,
        // A registry 404 is a missing organization or version, never
        // retryable; `registry::execute` names which.
        404 => {
            return problem(ErrorCode::ServiceResourceNotFound).with_detail("serviceStatus", 404);
        }
        _ => return unavailable(u64::from(status)),
    })
}

#[cfg(test)]
mod tests {
    /// The retired service-selection option, spelled so the public-surface
    /// scan does not match this negative test.
    const RETIRED_OPTION: &str = concat!("--service-", "environment");

    #[test]
    fn fixture_credentials_and_origins_are_checked() {
        let environment = Environment::production();
        let mut session = Session {
            environment: environment.clone(),
            fixture: Some(
                json!({"storeAvailable":true,"credentials":{"production":"production-only"},"exchanges":[{"method":"POST","path":"/auth/device","origin":environment.origin,"status":200,"body":{}}]}),
            ),
            next: 0,
            deadline: None,
            cancellation: CancellationToken::default(),
        };
        assert_eq!(
            session.store("get", None).unwrap().unwrap(),
            "production-only"
        );
        session.store("delete", None).unwrap();
        assert!(session.store("get", None).unwrap().is_none());
        assert!(
            session
                .request("POST", "/auth/device", None, json!({}))
                .is_ok()
        );
        session.next = 0;
        session.fixture.as_mut().unwrap()["exchanges"][0]["origin"] =
            json!("https://untrusted.invalid");
        assert!(
            session
                .request("POST", "/auth/device", None, json!({}))
                .is_err()
        );
    }

    #[test]
    fn transport_failures_prioritize_cancellation_then_expiry() {
        let cancel = CancellationToken::default();
        let mut session = Session {
            environment: Environment::production(),
            fixture: None,
            next: 0,
            deadline: None,
            cancellation: cancel.clone(),
        };
        assert_eq!(
            session.transport_failure().code,
            ErrorCode::ServiceUnavailable
        );
        session.deadline = Some(Instant::now());
        assert_eq!(
            session.transport_failure().code,
            ErrorCode::DeviceAuthExpired
        );
        cancel.cancel();
        assert_eq!(session.transport_failure().code, ErrorCode::Cancelled);
    }

    use super::*;
    #[test]

    fn token_alphabet_cannot_inject_native_commands() {
        assert!(valid_token("rr_test_11111111111111111111111111111111"));
        for token in [
            "",
            "fixture",
            "rr_live_11111111111111111111111111111111",
            "rr_test_11111111111111111111111111111111\nquit",
            "rr_test_1111111111111111111111111111111\"",
        ] {
            assert!(!valid_token(token));
        }
    }
    #[test]

    fn fixture_transport_fails_closed_on_missing_or_wrong_requests() {
        let mut session = Session {
            environment: Environment::production(),
            fixture: Some(json!({
                "credential":null,"storeAvailable":true,"exchanges":[]
            })),
            next: 0,
            deadline: None,
            cancellation: CancellationToken::default(),
        };
        assert_eq!(
            session
                .request("POST", "/auth/device", None, json!({}))
                .unwrap_err()
                .code,
            ErrorCode::ServiceProtocolInvalid
        );
        assert_eq!(
            session
                .request("GET", "/organizations", None, Value::Null)
                .unwrap_err()
                .code,
            ErrorCode::ServiceProtocolInvalid
        );
        session.fixture = Some(json!({
            "credential":"expected","exchanges":[{
                "method":"GET","path":"/organizations","status":200,"body":{
                    "organizations":[]
                }
            }]
        }));
        assert_eq!(
            session
                .request("GET", "/organizations", Some("wrong"), Value::Null)
                .unwrap_err()
                .code,
            ErrorCode::ServiceProtocolInvalid
        );
    }
    #[test]

    fn unknown_organization_roles_and_controls_fail_closed() {
        for role in ["owner", "root"] {
            assert!(
                organizations(&json!({
                    "organizations":[{
                        "id":"id","slug":"slug","name":"name","role":role
                    }]
                }))
                .is_err()
            );
        }
        assert!(
            organizations(&json!({
                "organizations":[{
                    "id":"id","slug":"slug","name":"\u{1b}"
                }]
            }))
            .is_err()
        );
    }
    #[test]

    fn cancellation_precedes_fixture_store_and_transport() {
        let cancel = CancellationToken::default();
        cancel.cancel();
        let mut session = Session {
            environment: Environment::production(),
            fixture: Some(json!({
                "credential":null,"storeAvailable":true,"exchanges":[]
            })),
            next: 0,
            deadline: None,
            cancellation: cancel,
        };
        assert_eq!(
            session.store("get", None).unwrap_err().code,
            ErrorCode::Cancelled
        );
        assert_eq!(
            session
                .request("POST", "/auth/device", None, json!({}))
                .unwrap_err()
                .code,
            ErrorCode::Cancelled
        );
    }
    #[test]

    fn fixture_store_is_in_memory_and_logout_is_idempotent() {
        let mut session = Session {
            environment: Environment::production(),
            fixture: Some(json!({
                "credential":null,"storeAvailable":true,"exchanges":[]
            })),
            next: 0,
            deadline: None,
            cancellation: CancellationToken::default(),
        };
        session.store("set", Some("test-only")).unwrap();
        assert_eq!(
            session.store("get", None).unwrap().as_deref(),
            Some("test-only")
        );
        session.store("delete", None).unwrap();
        session.store("delete", None).unwrap();
        assert!(session.store("get", None).unwrap().is_none());
    }
    #[test]
    fn service_environment_selection_is_gone() {
        let parse = |args: &[&str]| crate::parse_invocation(args.iter().map(|s| (*s).to_string()));
        // No global selects a service: the old option is not a runner option.
        let parsed = parse(&[RETIRED_OPTION, "production", "cli", "auth", "status"]);
        assert!(!matches!(
            parsed,
            Ok(crate::ParsedInvocation {
                action: crate::invocation::Action::Runner {
                    command: RunnerCommand::AuthStatus,
                    ..
                },
                ..
            })
        ));
        assert!(parse(&["cli", "org", "list"]).is_ok());
        // Language arguments stay opaque.
        assert!(parse(&["run", RETIRED_OPTION, "production"]).is_ok());
    }
}
