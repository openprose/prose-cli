use crate::error::RunnerError;
use serde::Serialize;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum OutputMode {
    #[default]
    Human,
    Json,
    Jsonl,
}

impl OutputMode {
    /// Parses the closed machine-output vocabulary.
    ///
    /// # Errors
    ///
    /// Returns `INVOCATION_INVALID` when `value` is not a supported mode.
    pub fn parse(value: &str) -> Result<Self, RunnerError> {
        match value {
            "human" => Ok(Self::Human),
            "json" => Ok(Self::Json),
            "jsonl" => Ok(Self::Jsonl),
            _ => Err(RunnerError::config(format!(
                "invalid output mode {value:?}; expected human, json, or jsonl"
            ))),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GlobalFlags {
    pub service_environment: Option<String>,
    pub harness: Option<String>,
    pub transport: Option<String>,
    pub cwd: Option<PathBuf>,
    pub model: Option<String>,
    pub auth_profile: Option<String>,
    pub native_log: Option<String>,
    pub output_contract: Option<String>,
    pub permission_mode: Option<String>,
    pub native_profile: Option<String>,
    pub native_max_turns: Option<String>,
    pub native_timeout: Option<String>,
    pub native_tool_timeout: Option<String>,
    pub native_output_bytes: Option<String>,
    pub native_add_dirs: Vec<String>,
    pub native_allow_tools: Vec<String>,
    pub timeout: Option<String>,
    pub output: Option<OutputMode>,
    pub dry_run: bool,
    pub no_color: bool,
    pub verbose: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunnerCommand {
    Doctor,
    HarnessList,
    HarnessUse(String),
    CleanupPrime(String),
    ConfigExplain,
    AuthStatus,
    AuthLogin,
    AuthLogout,
    OrgList,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    Help,
    Version,
    Runner {
        command: RunnerCommand,
        json: bool,
    },
    Forward {
        /// Always begins with the literal `prose` introducer.
        argv: Vec<String>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedInvocation {
    pub globals: GlobalFlags,
    pub action: Action,
}

/// Parses the closed runner-global prefix. The first language token or `--`
/// permanently freezes global parsing and keeps all subsequent language
/// arguments opaque. The reserved `cli harness use` runner command separately
/// accepts model/auth selection options after its harness ID.
///
/// # Errors
///
/// Returns `INVOCATION_INVALID` at the invocation boundary for malformed
/// runner-owned options or commands.
pub fn parse_invocation(
    args: impl IntoIterator<Item = String>,
) -> Result<ParsedInvocation, RunnerError> {
    let parsed = parse_invocation_inner(args)?;
    if parsed.globals.service_environment.is_some() {
        let mut other = parsed.globals.clone();
        other.service_environment = None; other.output = None; other.no_color = false; other.verbose = false;
        if other != GlobalFlags::default() || !matches!(parsed.action,
            Action::Runner { command: RunnerCommand::AuthStatus | RunnerCommand::AuthLogin | RunnerCommand::AuthLogout | RunnerCommand::OrgList, .. }) {
            return Err(RunnerError::invocation("service environment requires an account or organization command"));
        }
    }
    if parsed.globals.service_environment.is_none()
        && matches!(parsed.action, Action::Runner { command: RunnerCommand::OrgList, .. }) {
        return Err(RunnerError::invocation("organization commands require the staging service environment"));
    }
    Ok(parsed)
}

fn parse_invocation_inner(args: impl IntoIterator<Item = String>) -> Result<ParsedInvocation, RunnerError> {
    let args: Vec<String> = args.into_iter().collect();
    let mut globals = GlobalFlags::default();
    let mut index = 0;

    while index < args.len() {
        let token = &args[index];
        if token == "--" {
            let opaque = args[index + 1..].to_vec();
            if opaque.is_empty() {
                return Err(RunnerError::invocation(
                    "`--` must be followed by a language command",
                ));
            }
            return Ok(ParsedInvocation {
                globals,
                action: forward(opaque),
            });
        }

        if token == "--help" {
            return Ok(ParsedInvocation {
                globals,
                action: Action::Help,
            });
        }
        if token == "--version" {
            return Ok(ParsedInvocation {
                globals,
                action: Action::Version,
            });
        }

        if token == "--dry-run" {
            globals.dry_run = true;
            index += 1;
            continue;
        }
        if token == "--no-color" {
            globals.no_color = true;
            index += 1;
            continue;
        }
        if token == "--verbose" {
            globals.verbose = true;
            index += 1;
            continue;
        }

        if let Some((name, value)) = split_option(token) {
            set_value_option(&mut globals, name, value)?;
            index += 1;
            continue;
        }

        if is_value_option(token) {
            let value = args.get(index + 1).ok_or_else(|| {
                RunnerError::invocation(format!("runner option {token} requires a value"))
            })?;
            set_value_option(&mut globals, token, value)?;
            index += 2;
            continue;
        }

        // Unknown options are deliberately not rejected here. They are the
        // first opaque language token and freeze runner parsing.
        let remaining = args[index..].to_vec();
        if token == "cli" {
            let action = parse_runner_command(&remaining[1..], &mut globals)?;
            return Ok(ParsedInvocation { globals, action });
        }
        return Ok(ParsedInvocation {
            globals,
            action: forward(remaining),
        });
    }

    Ok(ParsedInvocation {
        globals,
        action: Action::Help,
    })
}

/// Recognizes only the reserved weave route, preserving the ordinary parser's
/// prefix freeze and early identity/help behavior. A true flag means prohibited
/// globals were supplied. Invalid prefixes remain owned by `parse_invocation`.
#[must_use]
pub fn weave_route(args: &[String]) -> Option<(bool, &[String])> {
    let mut index = 0;
    let mut globals = GlobalFlags::default();
    while let Some(token) = args.get(index) {
        if matches!(token.as_str(), "--" | "--help" | "--version") { return None; }
        if matches!(token.as_str(), "--dry-run" | "--no-color" | "--verbose") { index += 1; continue; }
        if let Some((name, value)) = split_option(token) {
            set_value_option(&mut globals, name, value).ok()?; index += 1; continue;
        }
        if is_value_option(token) {
            set_value_option(&mut globals, token, args.get(index + 1)?).ok()?; index += 2; continue;
        }
        return (token == "cli" && args.get(index + 1).is_some_and(|v| v == "weave"))
            .then(|| (index != 0, &args[index + 2..]));
    }
    None
}

fn forward(mut opaque: Vec<String>) -> Action {
    let mut argv = Vec::with_capacity(opaque.len() + 1);
    argv.push("prose".to_owned());
    argv.append(&mut opaque);
    Action::Forward { argv }
}

fn is_value_option(value: &str) -> bool {
    matches!(
        value,
        "--service-environment"
            | "--harness"
            | "--transport"
            | "--cwd"
            | "--model"
            | "--auth-profile"
            | "--native-max-turns"
            | "--native-timeout"
            | "--native-tool-timeout"
            | "--native-output-bytes"
            | "--native-profile"
            | "--native-add-dir"
            | "--native-allow-tool"
            | "--native-log"
            | "--output-contract"
            | "--permission-mode"
            | "--timeout"
            | "--output"
    )
}

fn split_option(value: &str) -> Option<(&str, &str)> {
    let (name, option_value) = value.split_once('=')?;
    is_value_option(name).then_some((name, option_value))
}

fn set_value_option(globals: &mut GlobalFlags, name: &str, value: &str) -> Result<(), RunnerError> {
    if value.is_empty() {
        return Err(RunnerError::invocation(format!(
            "runner option {name} requires a non-empty value"
        )));
    }
    match name {
        "--service-environment" => {
            if value != "staging" { return Err(RunnerError::invocation("service environment must be staging")); }
            set_once(&mut globals.service_environment, name, value)?;
        }
        "--harness" => globals.harness = Some(value.to_owned()),
        "--transport" => globals.transport = Some(value.to_owned()),
        "--cwd" => globals.cwd = Some(PathBuf::from(value)),
        "--model" => set_once(&mut globals.model, name, value)?,
        "--auth-profile" => set_once(&mut globals.auth_profile, name, value)?,
        "--native-log" => set_once(&mut globals.native_log,name,value)?,
        "--output-contract" => {
            if !matches!(value,"native"|"image-envelope") { return Err(RunnerError::invocation("output contract must be native or image-envelope")); }
            set_once(&mut globals.output_contract,name,value)?;
        }
        "--native-max-turns" => set_once(&mut globals.native_max_turns, name, value)?,
        "--native-timeout" => set_once(&mut globals.native_timeout, name, value)?,
        "--native-tool-timeout" => set_once(&mut globals.native_tool_timeout, name, value)?,
        "--native-output-bytes" => set_once(&mut globals.native_output_bytes, name, value)?,
        "--native-profile" => set_once(&mut globals.native_profile, name, value)?,
        "--native-add-dir" => globals.native_add_dirs.push(value.to_owned()),
        "--native-allow-tool" => globals.native_allow_tools.push(value.to_owned()),
        "--permission-mode" => set_once(&mut globals.permission_mode, name, value)?,
        "--timeout" => globals.timeout = Some(value.to_owned()),
        "--output" => {
            globals.output = Some(OutputMode::parse(value).map_err(|_| {
                RunnerError::invocation(format!(
                    "invalid output mode {value:?}; expected human, json, or jsonl"
                ))
            })?);
        }
        _ => unreachable!("checked by is_value_option"),
    }
    Ok(())
}

fn set_once(target: &mut Option<String>, name: &str, value: &str) -> Result<(), RunnerError> {
    if target.is_some() {
        return Err(RunnerError::invocation(format!(
            "runner option {name} was specified more than once"
        )));
    }
    *target = Some(value.to_owned());
    Ok(())
}

fn parse_runner_command(args: &[String], globals: &mut GlobalFlags) -> Result<Action, RunnerError> {
    if known_runner_help_path(args) {
        return Ok(Action::Help);
    }
    let (command, tail) = match args {
        [doctor, tail @ ..] if doctor == "doctor" => (RunnerCommand::Doctor, tail),
        [harness, list, tail @ ..] if harness == "harness" && list == "list" => {
            (RunnerCommand::HarnessList, tail)
        }
        [harness, use_command, harness_id, tail @ ..]
            if harness == "harness" && use_command == "use" =>
        {
            if !matches!(
                harness_id.as_str(),
                "openprose" | "prime" | "omp" | "codex" | "claude"
            ) {
                return Err(RunnerError::invocation(
                    "Harness selection must be one of openprose, prime, omp, codex, or claude.",
                ));
            }
            (RunnerCommand::HarnessUse(harness_id.clone()), tail)
        }
        [cleanup, prime, handle, tail @ ..] if cleanup == "cleanup" && prime == "prime" => {
            (RunnerCommand::CleanupPrime(handle.clone()), tail)
        }
        [config, explain, tail @ ..] if config == "config" && explain == "explain" => {
            (RunnerCommand::ConfigExplain, tail)
        }
        [org, list, tail @ ..] if org == "org" && list == "list" => (RunnerCommand::OrgList, tail),
        [auth, status, tail @ ..] if auth == "auth" && status == "status" => {
            (RunnerCommand::AuthStatus, tail)
        }
        [auth, login, tail @ ..] if auth == "auth" && login == "login" => {
            (RunnerCommand::AuthLogin, tail)
        }
        [auth, logout, tail @ ..] if auth == "auth" && logout == "logout" => {
            (RunnerCommand::AuthLogout, tail)
        }
        _ => {
            return Err(RunnerError::invocation(
                "unknown runner command; expected `cli doctor`, `cli harness list`, `cli harness use <id>`, `cli cleanup prime <handle>`, `cli config explain`, or `cli auth <status|login|logout>`",
            ));
        }
    };

    let json = if matches!(command, RunnerCommand::HarnessUse(_)) {
        parse_harness_use_options(tail, globals)?
    } else {
        match tail {
            [] => false,
            [flag] if flag == "--json" => true,
            _ => {
                return Err(RunnerError::invocation(
                    "runner command accepts only the optional `--json` flag",
                ));
            }
        }
    };

    if matches!(command, RunnerCommand::CleanupPrime(_)) && json {
        return Err(RunnerError::invocation(
            "Prime cleanup uses the global `--output json` option before `cli`",
        ));
    }

    Ok(Action::Runner { command, json })
}

fn parse_harness_use_options(
    args: &[String],
    globals: &mut GlobalFlags,
) -> Result<bool, RunnerError> {
    let mut json = false;
    let mut index = 0;
    while index < args.len() {
        let token = &args[index];
        if token == "--json" {
            if json {
                return Err(RunnerError::invocation(
                    "runner option --json was specified more than once",
                ));
            }
            json = true;
            index += 1;
            continue;
        }
        if let Some((name, value)) = split_option(token) {
            if matches!(name, "--model" | "--auth-profile") {
                set_value_option(globals, name, value)?;
                index += 1;
                continue;
            }
        }
        if matches!(token.as_str(), "--model" | "--auth-profile") {
            let value = args.get(index + 1).ok_or_else(|| {
                RunnerError::invocation(format!("runner option {token} requires a value"))
            })?;
            set_value_option(globals, token, value)?;
            index += 2;
            continue;
        }
        return Err(RunnerError::invocation(
            "`cli harness use` accepts only --model, --auth-profile, and --json after the harness ID",
        ));
    }
    Ok(json)
}

fn known_runner_help_path(args: &[String]) -> bool {
    let values = args.iter().map(String::as_str).collect::<Vec<_>>();
    matches!(
        values.as_slice(),
        ["--help"]
            | [
                "doctor" | "harness" | "cleanup" | "config" | "auth" | "org",
                "--help"
            ]
            | ["harness", "list" | "use", "--help"]
            | ["harness", "use", _, "--help"]
            | ["cleanup", "prime", "--help"]
            | ["cleanup", "prime", _, "--help"]
            | ["config", "explain", "--help"]
            | ["auth", "status" | "login" | "logout", "--help"]
            | ["org", "list", "--help"]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const INVOCATION_ACTION: &str = "Review the runner syntax with the --help option, place global options before cli, and retry the command.";

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    fn assert_invocation_error(args: &[&str], reason: &str) {
        let error = parse_invocation(strings(args)).unwrap_err();
        assert_eq!(error.code, crate::ErrorCode::InvocationInvalid, "{args:?}");
        assert_eq!(error.boundary, "invocation", "{args:?}");
        assert_eq!(error.message, "Runner invocation is invalid.", "{args:?}");
        assert_eq!(error.action, INVOCATION_ACTION, "{args:?}");
        assert_eq!(error.exit_code, 2, "{args:?}");
        assert!(!error.retryable, "{args:?}");
        assert_eq!(error.details.unwrap()["reason"], reason, "{args:?}");
    }

    #[test]
    fn malformed_runner_syntax_uses_the_invocation_boundary() {
        for (args, reason) in [
            (vec!["--"], "`--` must be followed by a language command"),
            (
                vec!["--output=machine", "cli", "doctor"],
                "invalid output mode \"machine\"; expected human, json, or jsonl",
            ),
            (
                vec!["--model", "one", "--model", "two", "cli", "doctor"],
                "runner option --model was specified more than once",
            ),
            (
                vec!["cli", "doctor", "extra"],
                "runner command accepts only the optional `--json` flag",
            ),
            (
                vec!["cli", "harness", "use", "nope"],
                "Harness selection must be one of openprose, prime, omp, codex, or claude.",
            ),
        ] {
            assert_invocation_error(&args, reason);
        }
    }

    #[test]
    fn freezes_globals_at_first_language_token() {
        let parsed = parse_invocation(strings(&[
            "--harness",
            "mock",
            "write",
            "--harness",
            "claude",
            "two words",
        ]))
        .unwrap();
        assert_eq!(parsed.globals.harness.as_deref(), Some("mock"));
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "write", "--harness", "claude", "two words"])
            }
        );
    }

    #[test]
    fn parses_auth_profile_only_in_the_runner_global_prefix() {
        let parsed = parse_invocation(strings(&[
            "--auth-profile=openrouter",
            "run",
            "--auth-profile",
            "language-value",
        ]))
        .unwrap();
        assert_eq!(parsed.globals.auth_profile.as_deref(), Some("openrouter"));
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "run", "--auth-profile", "language-value"])
            }
        );
    }

    #[test]
    fn preserves_hostile_argument_boundaries() {
        let opaque = strings(&[
            "write",
            "雪だるま ☃",
            "",
            "-leading",
            "line one\nline two",
            "$(touch nope)",
            "a; rm -rf /",
            "quote'\"`$HOME",
        ]);
        let parsed = parse_invocation(opaque.clone()).unwrap();
        let mut expected = vec!["prose".to_owned()];
        expected.extend(opaque);
        assert_eq!(parsed.action, Action::Forward { argv: expected });
    }

    #[test]
    fn delimiter_forces_cli_through_language_path() {
        let parsed = parse_invocation(strings(&["--", "cli", "doctor"])).unwrap();
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "cli", "doctor"])
            }
        );
    }

    #[test]
    fn bare_delimiter_is_rejected() {
        assert_invocation_error(&["--"], "`--` must be followed by a language command");
    }

    #[test]
    fn unknown_leading_option_is_opaque_language_input() {
        let parsed = parse_invocation(strings(&["--future-language-flag", "value"])).unwrap();
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "--future-language-flag", "value"])
            }
        );
    }

    #[test]
    fn parses_reserved_command_json_alias() {
        let parsed = parse_invocation(strings(&["cli", "config", "explain", "--json"])).unwrap();
        assert_eq!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::ConfigExplain,
                json: true
            }
        );
    }

    #[test]
    fn parses_user_harness_selection_without_forwarding_it() {
        let parsed = parse_invocation(strings(&[
            "--output", "json", "cli", "harness", "use", "claude",
        ]))
        .unwrap();
        assert_eq!(parsed.globals.output, Some(OutputMode::Json));
        assert_eq!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::HarnessUse("claude".to_owned()),
                json: false,
            }
        );
    }

    #[test]
    fn parses_harness_selection_suffix_flags_into_the_explicit_global_flags() {
        let parsed = parse_invocation(strings(&[
            "cli",
            "harness",
            "use",
            "prime",
            "--model",
            "openai/gpt-5.4",
            "--auth-profile=prime-harness-login",
            "--json",
        ]))
        .unwrap();
        assert_eq!(parsed.globals.model.as_deref(), Some("openai/gpt-5.4"));
        assert_eq!(
            parsed.globals.auth_profile.as_deref(),
            Some("prime-harness-login")
        );
        assert_eq!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::HarnessUse("prime".to_owned()),
                json: true,
            }
        );
    }

    #[test]
    fn harness_selection_rejects_duplicate_prefix_or_suffix_route_flags() {
        for args in [
            strings(&[
                "--model",
                "openai/first",
                "cli",
                "harness",
                "use",
                "prime",
                "--model",
                "openai/second",
            ]),
            strings(&[
                "cli",
                "harness",
                "use",
                "prime",
                "--auth-profile",
                "openai",
                "--auth-profile=openrouter",
            ]),
        ] {
            let error = parse_invocation(args).unwrap_err();
            assert_eq!(error.code, crate::ErrorCode::InvocationInvalid);
            assert_eq!(error.boundary, "invocation");
            assert!(
                error.details.unwrap()["reason"]
                    .as_str()
                    .unwrap()
                    .contains("specified more than once")
            );
        }
    }

    #[test]
    fn parses_prime_cleanup_only_through_the_global_output_surface() {
        let handle = "prime-v1.openprose-prime-Fixture1.018f47a6-7d2c-7b10-8a2e-1a2b3c4d5e6f";
        let parsed = parse_invocation(strings(&[
            "--output", "json", "cli", "cleanup", "prime", handle,
        ]))
        .unwrap();
        assert_eq!(parsed.globals.output, Some(OutputMode::Json));
        assert_eq!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::CleanupPrime(handle.to_owned()),
                json: false,
            }
        );
        assert_eq!(
            parse_invocation(strings(&["cli", "cleanup", "prime", handle, "--json"]))
                .unwrap_err()
                .boundary,
            "invocation"
        );
    }

    #[test]
    fn parses_hosted_account_operations() {
        for (name, command) in [
            ("status", RunnerCommand::AuthStatus),
            ("login", RunnerCommand::AuthLogin),
            ("logout", RunnerCommand::AuthLogout),
        ] {
            let parsed = parse_invocation(strings(&["cli", "auth", name])).unwrap();
            assert_eq!(
                parsed.action,
                Action::Runner {
                    command,
                    json: false
                }
            );
        }
    }

    #[test]
    fn known_runner_groups_and_commands_accept_help_without_configuration() {
        for args in [
            vec!["cli", "--help"],
            vec!["cli", "doctor", "--help"],
            vec!["cli", "harness", "--help"],
            vec!["cli", "harness", "list", "--help"],
            vec!["cli", "harness", "use", "--help"],
            vec!["cli", "harness", "use", "prime", "--help"],
            vec!["cli", "cleanup", "--help"],
            vec!["cli", "cleanup", "prime", "--help"],
            vec!["cli", "cleanup", "prime", "opaque-handle", "--help"],
            vec!["cli", "config", "--help"],
            vec!["cli", "config", "explain", "--help"],
            vec!["cli", "auth", "--help"],
            vec!["cli", "auth", "status", "--help"],
            vec!["cli", "auth", "login", "--help"],
            vec!["cli", "auth", "logout", "--help"],
        ] {
            let parsed = parse_invocation(strings(&args)).unwrap();
            assert_eq!(parsed.action, Action::Help, "{args:?}");
        }
    }

    #[test]
    fn help_does_not_make_unknown_runner_paths_valid() {
        for args in [
            vec!["cli", "unknown", "--help"],
            vec!["cli", "harness", "unknown", "--help"],
            vec!["cli", "doctor", "extra", "--help"],
        ] {
            let error = parse_invocation(strings(&args)).unwrap_err();
            assert_eq!(error.code, crate::ErrorCode::InvocationInvalid, "{args:?}");
            assert_eq!(error.boundary, "invocation", "{args:?}");
        }
    }
}

#[test]
fn parses_repeated_native_values_without_shell_interpretation(){
 let p=parse_invocation(vec!["--native-profile=claude-workspace-tools","--native-add-dir","a b","--native-add-dir=other","--native-allow-tool","Bash(git status:*)","run"].into_iter().map(str::to_owned)).unwrap();
 assert_eq!(p.globals.native_profile.as_deref(),Some("claude-workspace-tools"));
 assert_eq!(p.globals.native_add_dirs,vec!["a b","other"]);
 assert_eq!(p.globals.native_allow_tools,vec!["Bash(git status:*)"]);
}

#[cfg(test)]
mod sdk_budget_flag_tests {
 use super::*;
 #[test]
 fn sdk_budget_flags_reject_duplicates() {
  let p=parse_invocation(vec!["--native-max-turns=40","--native-timeout","5m","run"].into_iter().map(str::to_owned)).unwrap();
  assert_eq!(p.globals.native_max_turns.as_deref(),Some("40"));assert_eq!(p.globals.native_timeout.as_deref(),Some("5m"));
  assert!(parse_invocation(vec!["--native-max-turns=40","--native-max-turns=50"].into_iter().map(str::to_owned)).is_err());
 }
}

#[test]
fn sdk_tool_timeout_flag_is_a_runner_option() {
    let p=parse_invocation(vec!["--native-tool-timeout=1ms","run"].into_iter().map(str::to_owned)).unwrap();
    assert_eq!(p.globals.native_tool_timeout.as_deref(),Some("1ms"));
    assert!(parse_invocation(vec!["--native-tool-timeout","1s","--native-tool-timeout","2s"].into_iter().map(str::to_owned)).is_err());
}

#[test]
fn native_output_flag_is_single_valued(){
 let p=parse_invocation(vec!["--native-output-bytes=134217728","task"].into_iter().map(str::to_owned)).unwrap();
 assert_eq!(p.globals.native_output_bytes.as_deref(),Some("134217728"));
 assert!(parse_invocation(vec!["--native-output-bytes","1048576","--native-output-bytes","2097152","task"].into_iter().map(str::to_owned)).is_err());
}

#[cfg(test)]
mod weave_routing_tests {
    use super::*;
    #[test]
    fn opaque_paths_preserved() {
        // Shared corpus language-routing-47/48.
        for input in [vec!["weave","step","/config"],vec!["init","--host-binding","/binding"],vec!["--","cli","weave","step","/config"]] {
            let args:Vec<String>=input.iter().map(ToString::to_string).collect();
            assert!(weave_route(&args).is_none());
            let parsed=parse_invocation(args.clone()).unwrap();
            let mut expected=vec!["prose".to_owned()];expected.extend(args.into_iter().skip(usize::from(input[0]=="--")));
            assert_eq!(parsed.action,Action::Forward{argv:expected});
        }
    }
    #[test]
    fn recognized_route_rejects_globals_without_stealing_help_or_bad_prefix() {
        let args=|v:Vec<&str>|v.into_iter().map(str::to_owned).collect::<Vec<_>>();
        assert_eq!(weave_route(&args(vec!["cli","weave","--help"])),Some((false,args(vec!["--help"]).as_slice())));
        assert!(weave_route(&args(vec!["--dry-run","cli","weave","step"])).unwrap().0);
        for v in [vec!["--help","cli","weave"],vec!["--model","a","--model","b","cli","weave"],vec!["unknown","cli","weave"]] {assert!(weave_route(&args(v)).is_none());}
    }
}
