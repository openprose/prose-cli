//! Explicit staging account operations. Credentials never enter harness configuration.
use crate::{CancellationToken, OutputMode, RunnerCommand, RunnerError};
use crate::error::ErrorCode;
use crate::output::CommandOutcome;
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::time::{Duration, Instant};

const BASE: &str = "https://run-prose-staging.openprose.workers.dev";
const LIMIT: u64 = 65_536;

fn problem(code: ErrorCode) -> RunnerError {
    RunnerError::catalog(code)
}

fn valid_token(token: &str) -> bool {
    token.strip_prefix("rr_test_").is_some_and(|v| v.len() == 32 && v.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
}

struct Session {
    fixture: Option<Value>,
    next: usize,
    cancellation: CancellationToken,
    deadline: Option<Instant>,
}

impl Session {

    fn new(cancellation: &CancellationToken) -> Result<Self, RunnerError> {
        #[allow(unused_mut)]
        let mut fixture: Option<Value> = None;
        #[cfg(feature="test-seams")]
        if let Some(path) = std::env::var_os("PROSE_TEST_SERVICE_FIXTURE") {
            let mut bytes = Vec::new();
            std::fs::File::open(path).map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?.take(LIMIT + 1).read_to_end(&mut bytes).map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?;
            if bytes.len() as u64 > LIMIT {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            fixture = Some(serde_json::from_slice(&bytes).map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?);
        }
        if let Some(value) = &fixture {
            if !value.is_object()
                || !value["storeAvailable"].is_boolean()
                || !value["exchanges"].as_array().is_some_and(|v| v.len() <= 182)
                || !value.get("credential").is_some_and(|v| v.is_null() || v.is_string())
            {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
        }
        Ok(Self {
            fixture,
            next: 0,
            deadline: None,
            cancellation: cancellation.clone(),
        })
    }

    fn check(&self) -> Result<(), RunnerError> {
        if self.cancellation.is_cancelled() {
            Err(problem(ErrorCode::Cancelled))
        } else if self.deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            Err(problem(ErrorCode::DeviceAuthExpired))
        } else {
            Ok(())
        }
    }

    fn transport_failure(&self) -> RunnerError {
        self.check().err().unwrap_or_else(|| problem(ErrorCode::ServiceUnavailable))
    }

    fn store(&mut self, operation: &str, token: Option<&str>) -> Result<Option<String>, RunnerError> {
        self.check()?;
        if let Some(fixture) = self.fixture.as_mut() {
            if fixture["storeAvailable"] == false {
                return Err(problem(ErrorCode::CredentialStoreUnavailable));
            }
            let previous = fixture["credential"].as_str().map(str::to_owned);
            if operation == "set" {
                fixture["credential"] = json!(token);
            }
            if operation == "delete" {
                fixture["credential"] = Value::Null;
            }
            return Ok(previous);
        }
        native_store(operation, token, &self.cancellation)
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
            if (path == "/organizations") != token.is_some() || (path != "/organizations" && token.is_some()) {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            if let Some(token) = token {
                let expected = std::env::var("OPENPROSE_STAGING_API_KEY").ok().or_else(|| fixture["credential"].as_str().map(str::to_owned));
                if expected.as_deref() != Some(token) {
                    return Err(problem(ErrorCode::ServiceProtocolInvalid));
                }
            }
            let exchange = fixture["exchanges"].get(self.next).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            self.next += 1;
            if exchange["method"] != method || exchange["path"] != path {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            (exchange["status"].as_u64().unwrap_or(0), exchange["body"].clone())
        } else {
            let timeout = self.deadline.map_or(Duration::from_secs(10), |deadline| deadline.saturating_duration_since(Instant::now()).min(Duration::from_secs(10)));
            if timeout.is_zero() {
                return Err(problem(ErrorCode::DeviceAuthExpired));
            }
            let agent = ureq::AgentBuilder::new().timeout(timeout).redirects(0).build();
            let mut request = agent.request(method, &format!("{BASE}{path}")).set("Accept", "application/json");
            if let Some(token) = token {
                request = request.set("Authorization", &format!("Bearer {token}"));
            }
            let result = if method == "POST" {
                request.set("Content-Type", "application/json").send_string(&body.to_string())
            } else {
                request.call()
            };
            let response = match result {
                Ok(r) | Err(ureq::Error::Status(_,r)) => r, Err(_) => return Err(self.transport_failure())
            };
            self.check()?;
            let status = u64::from(response.status());
            if matches!(status,401|403) {
                return Err(problem(ErrorCode::ServiceAuthRequired));
            }
            if !(200..=299).contains(&status) && !(status == 400 && path.ends_with("/poll")) {
                return Err(problem(ErrorCode::ServiceUnavailable));
            }
            let mut bytes = Vec::new();
            response.into_reader().take(LIMIT+1).read_to_end(&mut bytes).map_err(|_| self.transport_failure())?;
            self.check()?;
            if bytes.len() as u64 > LIMIT {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            (status, serde_json::from_slice(&bytes).map_err(|_| problem(ErrorCode::ServiceProtocolInvalid))?)
        };
        self.check()?;
        match status {
            200..=299 => Ok(value), 401 | 403 => Err(problem(ErrorCode::ServiceAuthRequired)), 400 if path.ends_with("/poll") => Err(problem(if value["error"] == "expired_token" {
                ErrorCode::DeviceAuthExpired
            } else {
                ErrorCode::DeviceAuthFailed
            })), _ => Err(problem(ErrorCode::ServiceUnavailable))
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

fn organizations(value: Value) -> Result<Value, RunnerError> {
    let entries = value["organizations"].as_array().ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
    if entries.len()>1000 {
        return Err(problem(ErrorCode::ServiceProtocolInvalid));
    }
    let mut output = Vec::new();
    for entry in entries {
        let mut row = json!({
        });
        for field in ["id", "slug", "name"] {
            let text = entry[field].as_str().filter(|s| !s.is_empty() && s.chars().count()<=4096 && !s.chars().any(|c| c <= '\u{1f}' || c == '\u{7f}')).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            row[field] = json!(text);
        }
        if let Some(role) = entry.get("role") {
            if !matches!(role.as_str(),Some("admin"|"developer"|"reader")) {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            row["role"] = role.clone();
        }
        output.push(row);
    }
    Ok(json!(output))
}
/// Executes an account operation with a bounded staging transport.
#[must_use]
pub fn execute(
    command: &RunnerCommand,
    mode: OutputMode,
    cancellation: &CancellationToken,
) -> CommandOutcome {
    let operation = match command {
        RunnerCommand::AuthLogin => "login", RunnerCommand::AuthLogout => "logout", RunnerCommand::OrgList => "list", _ => "status"
    };
    let mut source = "none";
    let mut authenticated = false;
    let mut rows = json!([]);
    let result = (|| -> Result<(),RunnerError> {
        let mut session = Session::new(cancellation)?;
        let environment = std::env::var("OPENPROSE_STAGING_API_KEY").ok().filter(|s| !s.is_empty());
        if environment.is_some() && matches!(operation,"login"|"logout") {
            return Err(RunnerError::invocation("Environment credentials cannot be changed by login or logout."));
        }
        if operation == "logout" {
            session.store("delete",None)?;
            return Ok(());
        }
        if operation == "login" {
            session.store("get",None)?;
            let start = session.request("POST","/auth/device",None,json!({
            }))?;
            let code = start["device_code"].as_str().filter(|s| !s.is_empty() && s.len()<=1024).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?.to_owned();
            let user = start["user_code"].as_str().filter(|s| !s.is_empty() && s.len()<=32 && s.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'-') && !s.contains(&code)).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            if start["verification_uri"] != "https://github.com/login/device" {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            let expiry = start["expires_in"].as_u64().filter(|v| (1..=900).contains(v)).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            let mut interval = start["interval"].as_u64().filter(|v| (1..=30).contains(v)).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
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
                let poll = session.request("POST","/auth/device/poll",None,json!({
                    "device_code":code
                }))?;
                if began.elapsed().as_secs() >= expiry {
                    return Err(problem(ErrorCode::DeviceAuthExpired));
                }
                match poll["status"].as_str() {
                    Some("pending") => {
                    },
                    Some("slow_down") => interval = (interval+5).min(30),
                    Some("complete") => {
                        let token = poll["api_key"].as_str().filter(|s| valid_token(s)).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
                        session.store("set",Some(token))?;
                        if session.store("get",None)?.as_deref() != Some(token) {
                            return Err(problem(ErrorCode::CredentialStoreUnavailable));
                        }
                        source = "os-credential-store";
                        authenticated = true;
                        return Ok(());
                    },
                    Some("error") if poll["error"] == "expired_token" => return Err(problem(ErrorCode::DeviceAuthExpired)),
                    Some("error") => return Err(problem(ErrorCode::DeviceAuthFailed)),
                    _ => return Err(problem(ErrorCode::ServiceProtocolInvalid)),
                }
            }
            return Err(problem(ErrorCode::DeviceAuthExpired));
        }
        let token = if let Some(token) = environment {
            source = "environment";
            Some(token)
        } else {
            let token = session.store("get",None)?;
            if token.is_some() {
                source = "os-credential-store";
            }
            token
        };
        if let Some(token) = token {
            if !valid_token(&token) {
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            rows = organizations(session.request("GET","/organizations",Some(&token),Value::Null)?)?;
            if rows.as_array().is_some_and(|entries| entries.iter().any(|entry| ["id","slug","name"].iter().any(|key| entry[*key].as_str().is_some_and(|value| value.contains(&token))))) {
                rows = json!([]);
                return Err(problem(ErrorCode::ServiceProtocolInvalid));
            }
            authenticated = true;
        } else if operation == "list" {
            return Err(problem(ErrorCode::ServiceAuthRequired));
        }
        Ok(())
    })();
    let error = result.err();
    if error.is_some() {
        source = "none";
    }
    let exit = error.as_ref().map_or(0,|e| e.exit_code);
    let report = if operation == "list" {
        json!({
            "schema":"openprose.organization-list/1","environment":"staging","organizations":rows,"problem":error
        })
    } else {
        json!({
            "schema":"openprose.service-account/1","environment":"staging","operation":operation,"authenticated":authenticated,"credentialSource":source,"problem":error
        })
    };
    if mode == OutputMode::Human {
        if let Some(error) = error {
            CommandOutcome::human("",format!("{}: {}\n",error.code,error.message),exit)
        }
        else if operation == "list" {
            CommandOutcome::human(format!("{}\n",rows),"",0)
        }
        else {
            CommandOutcome::human(format!("Staging account {operation}: {}\n", if authenticated {
                "authenticated"
            } else {
                "signed out"
            }),"",0)
        }
    } else {
        CommandOutcome::json(report,exit)
    }
}
#[cfg(not(target_os = "macos"))]

fn native_store(_: &str, _: Option<&str>, _: &CancellationToken) -> Result<Option<String>, RunnerError> {
    Err(problem(ErrorCode::CredentialStoreUnavailable))
}
#[cfg(target_os = "macos")]

fn native_store(
    operation: &str,
    token: Option<&str>,
    cancellation: &CancellationToken,
) -> Result<Option<String>, RunnerError> {
    use std::process::{
        Command, Stdio
    };
    // No shell or token argv. Interactive security command parsing receives only
    // fixed commands and a closed ASCII token alphabet over an anonymous pipe.
    let arguments = "-s org.openprose.cli.staging -a api-key";
    let script = match operation {
        "get" => format!("find-generic-password {arguments} -w\n"),
        "delete" => format!("delete-generic-password {arguments}\n"),
        "set" => {
            let token = token.filter(|s| valid_token(s)).ok_or_else(|| problem(ErrorCode::ServiceProtocolInvalid))?;
            format!("add-generic-password -U {arguments} -w {token}\n")
        },
        _ => return Err(problem(ErrorCode::CredentialStoreUnavailable)),
    };
    let mut child = Command::new("/usr/bin/security").arg("-i").env_clear()
    .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()
    .map_err(|_| problem(ErrorCode::CredentialStoreUnavailable))?;
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let read = |pipe: Box<dyn Read + Send>| std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = pipe.take(8193).read_to_end(&mut bytes);
        (result.is_ok() && bytes.len() <= 8192, bytes)
    });
    let out_reader = read(Box::new(stdout));
    let err_reader = read(Box::new(stderr));
    let write_ok = child.stdin.take().is_some_and(|mut stdin| stdin.write_all(script.as_bytes()).is_ok());
    let deadline = Instant::now()+Duration::from_secs(10);
    let mut failed = !write_ok;
    loop {
        if cancellation.is_cancelled() || Instant::now() >= deadline || failed {
            failed = true;
            let _ = child.kill();
            let _ = child.wait();
            break;
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                failed = !status.success();
                break;
            },
            Ok(None) => std::thread::sleep(Duration::from_millis(25)),
            Err(_) => {
                failed = true;
                let _=child.kill();
                let _=child.wait();
                break;
            },
        }
    }
    let (out_ok, stdout) = out_reader.join().unwrap_or_default();
    let (err_ok, stderr) = err_reader.join().unwrap_or_default();
    if cancellation.is_cancelled() {
        return Err(problem(ErrorCode::Cancelled));
    }
    if !out_ok || !err_ok {
        return Err(problem(ErrorCode::CredentialStoreUnavailable));
    }
    // security -i may exit zero after a failed subcommand. Inspect only fixed
    // error markers; never propagate its output or captured command echo.
    let diagnostic = String::from_utf8_lossy(&stderr);
    if diagnostic.contains("could not be found") && matches!(operation,"get"|"delete") {
        return Ok(None);
    }
    if failed || diagnostic.contains("SecKeychain") || diagnostic.contains("SecItem") || diagnostic.contains("error:") {
        return Err(problem(ErrorCode::CredentialStoreUnavailable));
    }
    if operation == "get" {
        let output = String::from_utf8(stdout).map_err(|_| problem(ErrorCode::CredentialStoreUnavailable))?;
        let token = output.trim();
        if !valid_token(token) {
            return Err(problem(ErrorCode::CredentialStoreUnavailable));
        }
        Ok(Some(token.to_owned()))
    } else {
        Ok(None)
    }
}
#[cfg(test)]
mod tests {
    #[test]
    fn transport_failures_prioritize_cancellation_then_expiry() {
        let cancel = CancellationToken::default();
        let mut session = Session { fixture: None, next: 0, deadline: None, cancellation: cancel.clone() };
        assert_eq!(session.transport_failure().code, ErrorCode::ServiceUnavailable);
        session.deadline = Some(Instant::now());
        assert_eq!(session.transport_failure().code, ErrorCode::DeviceAuthExpired);
        cancel.cancel();
        assert_eq!(session.transport_failure().code, ErrorCode::Cancelled);
    }

    use super::*;
    #[test]

    fn token_alphabet_cannot_inject_native_commands() {
        assert!(valid_token("rr_test_11111111111111111111111111111111"));
        for token in ["", "fixture", "rr_live_11111111111111111111111111111111", "rr_test_11111111111111111111111111111111\nquit", "rr_test_1111111111111111111111111111111\""] {
            assert!(!valid_token(token));
        }
    }
    #[test]

    fn fixture_transport_fails_closed_on_missing_or_wrong_requests() {
        let mut session = Session {
            fixture: Some(json!({
                "credential":null,"storeAvailable":true,"exchanges":[]
            })), next:0, deadline:None, cancellation:CancellationToken::default()
        };
        assert_eq!(session.request("POST","/auth/device",None,json!({
        })).unwrap_err().code,ErrorCode::ServiceProtocolInvalid);
        assert_eq!(session.request("GET","/organizations",None,Value::Null).unwrap_err().code,ErrorCode::ServiceProtocolInvalid);
        session.fixture = Some(json!({
            "credential":"expected","exchanges":[{
                "method":"GET","path":"/organizations","status":200,"body":{
                    "organizations":[]
                }
            }]
        }));
        assert_eq!(session.request("GET","/organizations",Some("wrong"),Value::Null).unwrap_err().code,ErrorCode::ServiceProtocolInvalid);
    }
    #[test]

    fn unknown_organization_roles_and_controls_fail_closed() {
        for role in ["owner", "root"] {
            assert!(organizations(json!({
                "organizations":[{
                    "id":"id","slug":"slug","name":"name","role":role
                }]
            })).is_err());
        }
        assert!(organizations(json!({
            "organizations":[{
                "id":"id","slug":"slug","name":"\u{1b}"
            }]
        })).is_err());
    }
    #[test]

    fn cancellation_precedes_fixture_store_and_transport() {
        let cancel = CancellationToken::default();
        cancel.cancel();
        let mut session = Session {
            fixture: Some(json!({
                "credential":null,"storeAvailable":true,"exchanges":[]
            })), next:0, deadline:None, cancellation:cancel
        };
        assert_eq!(session.store("get",None).unwrap_err().code,ErrorCode::Cancelled);
        assert_eq!(session.request("POST","/auth/device",None,json!({
        })).unwrap_err().code,ErrorCode::Cancelled);
    }
    #[test]

    fn fixture_store_is_in_memory_and_logout_is_idempotent() {
        let mut session = Session {
            fixture: Some(json!({
                "credential":null,"storeAvailable":true,"exchanges":[]
            })), next:0, deadline:None, cancellation:CancellationToken::default()
        };
        session.store("set",Some("test-only")).unwrap();
        assert_eq!(session.store("get",None).unwrap().as_deref(),Some("test-only"));
        session.store("delete",None).unwrap();
        session.store("delete",None).unwrap();
        assert!(session.store("get",None).unwrap().is_none());
    }
    #[test]

    fn staging_flag_preserves_language_boundary_and_rejects_other_operations() {
        let parse = |args: &[&str]| crate::parse_invocation(args.iter().map(|s| s.to_string()));
        assert!(parse(&["--service-environment","staging","cli","org","list"]).is_ok());
        assert!(parse(&["--service-environment","staging","cli","doctor"]).is_err());
        assert!(parse(&["--service-environment","production","cli","auth","status"]).is_err());
        let parsed = parse(&["run","--service-environment","staging"]).unwrap();
        assert!(parsed.globals.service_environment.is_none());
    }
}
