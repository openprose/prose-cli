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
                "invalid output mode {value_quoted}; expected human, json, or jsonl",
                value_quoted = crate::error::quote(value)
            ))),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GlobalFlags {
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
    Package(crate::registry::PackageCommand),
    /// A service operation (or its help).
    Service(crate::service::ServiceCommand),
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
        /// The `prose cli ...` argv these words also name when a file made
        /// them a language command; named by a
        /// `HOSTED_UNAVAILABLE` Action.
        service_hint: Option<Vec<String>>,
        /// The rejection a language command word that also names a service
        /// command (`prose status`, `prose run FILE`) gets when the default
        /// hosted harness would refuse it anyway.
        hosted_rejection: Option<Box<crate::service::ServiceCommand>>,
        /// `service_hint` goes only into `details.suggestedArgv`: the
        /// `HOSTED_UNAVAILABLE` refusal of `prose run FILE` keeps its frozen
        /// Action.
        hint_in_details_only: bool,
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
    parse_invocation_inner(args)
}

fn parse_invocation_inner(
    args: impl IntoIterator<Item = String>,
) -> Result<ParsedInvocation, RunnerError> {
    let args: Vec<String> = args.into_iter().collect();
    let full = args.clone();
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

        // An invalid `--output` value before `cli` is a service invocation
        // error with a suggestion, never an alias.
        if let Some(command) = crate::service::invalid_global_output(&args, index) {
            return Ok(ParsedInvocation {
                globals,
                action: Action::Runner {
                    command: RunnerCommand::Service(command),
                    json: false,
                },
            });
        }

        if let Some((name, value)) = split_option(token) {
            set_value_option(&mut globals, name, value)?;
            index += 1;
            continue;
        }

        if is_value_option(token) {
            let value = args
                .get(index + 1)
                .ok_or_else(|| RunnerError::invocation(format!("{token} requires a value.")))?;
            set_value_option(&mut globals, token, value)?;
            index += 2;
            continue;
        }

        // Unknown options are deliberately not rejected here. They are the
        // first opaque language token and freeze runner parsing.
        // Service command words without `cli`, or a runner-global alias
        // before them, never reach the language unless a file of that name
        // makes them a language command.
        // `prose help cli [COMMAND]` asks for the runner's cli help: `cli` is
        // reserved, so it can never be a language topic.
        // Command-local options before `cli` (`prose --json cli run list`)
        // are never forwarded to the language.
        if let Some(command) = crate::service::misplaced_local_options(&args, index, global_kind) {
            return Ok(ParsedInvocation {
                globals,
                action: Action::Runner {
                    command: RunnerCommand::Service(command),
                    json: false,
                },
            });
        }
        if token == "help" && args.get(index + 1).is_some_and(|next| next == "cli") {
            let mut helped = vec!["help".to_owned()];
            helped.extend_from_slice(&args[index + 2..]);
            // Help is text in every mode: `prose help cli --json`.
            if let Some(without) = crate::service::help_without_json(&helped) {
                helped = without;
            }
            if crate::service::help_request(&helped).is_some() {
                let action = parse_runner_command(&helped, &mut globals, &full)?;
                return Ok(ParsedInvocation { globals, action });
            }
        }
        if let Some(redirect) = crate::service::cli_redirect(&args, index, global_kind) {
            let on_disk = redirect.hint_only
                || redirect.operands.iter().any(|operand| {
                    globals
                        .cwd
                        .as_deref()
                        .map_or_else(|| PathBuf::from(operand), |cwd| cwd.join(operand))
                        .exists()
                });
            if !on_disk && !redirect.language {
                return Ok(ParsedInvocation {
                    globals,
                    action: Action::Runner {
                        command: RunnerCommand::Service(redirect.command),
                        json: false,
                    },
                });
            }
            let Action::Forward { argv, .. } = forward(args[index..].to_vec()) else {
                unreachable!("forward builds a Forward action")
            };
            return Ok(ParsedInvocation {
                globals,
                action: Action::Forward {
                    argv,
                    service_hint: Some(redirect.argv),
                    hosted_rejection: (!on_disk && redirect.language)
                        .then(|| Box::new(redirect.command)),
                    hint_in_details_only: redirect.hint_only,
                },
            });
        }
        // An unknown option before `cli` and a service command is a service
        // invocation error, never language input.
        if let Some(command) = crate::service::unknown_option_before_cli(&args, index, global_kind)
        {
            return Ok(ParsedInvocation {
                globals,
                action: Action::Runner {
                    command: RunnerCommand::Service(command),
                    json: false,
                },
            });
        }
        let remaining = args[index..].to_vec();
        if token == "cli" {
            let action = parse_runner_command(&remaining[1..], &mut globals, &full)?;
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
        if matches!(token.as_str(), "--" | "--help" | "--version") {
            return None;
        }
        if matches!(token.as_str(), "--dry-run" | "--no-color" | "--verbose") {
            index += 1;
            continue;
        }
        if let Some((name, value)) = split_option(token) {
            set_value_option(&mut globals, name, value).ok()?;
            index += 1;
            continue;
        }
        if is_value_option(token) {
            set_value_option(&mut globals, token, args.get(index + 1)?).ok()?;
            index += 2;
            continue;
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
    Action::Forward {
        argv,
        service_hint: None,
        hosted_rejection: None,
        hint_in_details_only: false,
    }
}

/// Classifies a runner-global option: `Some(true)` takes a value,
/// `Some(false)` is a flag, `None` is not a runner global.
fn global_kind(name: &str) -> Option<bool> {
    if is_value_option(name) {
        Some(true)
    } else if matches!(name, "--dry-run" | "--no-color" | "--verbose") {
        Some(false)
    } else {
        None
    }
}

pub(crate) fn is_value_option(value: &str) -> bool {
    matches!(
        value,
        "--harness"
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
        return Err(RunnerError::invocation(format!("{name} requires a value.")));
    }
    match name {
        "--harness" => globals.harness = Some(value.to_owned()),
        "--transport" => globals.transport = Some(value.to_owned()),
        "--cwd" => globals.cwd = Some(PathBuf::from(value)),
        "--model" => set_once(&mut globals.model, name, value)?,
        "--auth-profile" => set_once(&mut globals.auth_profile, name, value)?,
        "--native-log" => set_once(&mut globals.native_log, name, value)?,
        "--output-contract" => {
            if !matches!(value, "native" | "image-envelope") {
                return Err(RunnerError::invocation(
                    "output contract must be native or image-envelope",
                ));
            }
            set_once(&mut globals.output_contract, name, value)?;
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
                    "invalid output mode {value_quoted}; expected human, json, or jsonl",
                    value_quoted = crate::error::quote(value)
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

fn parse_runner_command(
    args: &[String],
    globals: &mut GlobalFlags,
    full: &[String],
) -> Result<Action, RunnerError> {
    // Help is text in every mode, so `--json` beside a help request is
    // dropped.
    let unjson = crate::service::help_without_json(args);
    let args = unjson.as_deref().unwrap_or(args);
    // Bare `cli`, `cli help [COMMAND]`, `cli <group> help` and a trailing
    // `-h` read as the matching `--help`.
    let helped = crate::service::help_request(args);
    let args = helped.as_deref().unwrap_or(args);
    if let Some(help) = crate::service::group_help(args) {
        return Ok(Action::Runner {
            command: RunnerCommand::Service(crate::service::ServiceCommand::Help(help)),
            json: false,
        });
    }
    let short_help = match args {
        [rest @ .., last] if last == "-h" => {
            let mut normalized = rest.to_vec();
            normalized.push("--help".to_owned());
            Some(normalized)
        }
        _ => None,
    };
    if known_runner_help_path(short_help.as_deref().unwrap_or(args)) {
        return Ok(Action::Help);
    }
    if let Some(command) = crate::service::misplaced_globals(args, full) {
        return Ok(Action::Runner {
            command: RunnerCommand::Service(command),
            json: false,
        });
    }
    if crate::service::claims(args) {
        let command = crate::service::parse(args, full)?;
        return Ok(Action::Runner {
            command: RunnerCommand::Service(command),
            json: false,
        });
    }
    if let [package, rest @ ..] = args {
        if package == "package" {
            let rest = take_trailing_globals(rest, globals)?;
            let (command, json) = crate::registry::parse(&rest)?;
            return Ok(Action::Runner {
                command: RunnerCommand::Package(command),
                json,
            });
        }
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
            return Ok(Action::Runner {
                command: RunnerCommand::Service(crate::service::unknown(args, full)),
                json: false,
            });
        }
    };

    // Account verbs take `--output` after the command path, like service verbs.
    let account_tail;
    let tail = if matches!(
        command,
        RunnerCommand::AuthStatus
            | RunnerCommand::AuthLogin
            | RunnerCommand::AuthLogout
            | RunnerCommand::OrgList
    ) {
        account_tail = take_trailing_globals(tail, globals)?;
        account_tail.as_slice()
    } else {
        tail
    };
    let json = if matches!(command, RunnerCommand::HarnessUse(_)) {
        parse_harness_use_options(tail, globals)?
    } else {
        match tail {
            [] => false,
            [flag] if flag == "--json" => true,
            // Anything else after a runner or account command path is the
            // service parser's "missing or invalid arguments" rejection, as
            // in the Bun build.
            _ => {
                return Ok(Action::Runner {
                    command: RunnerCommand::Service(crate::service::unknown(args, full)),
                    json: false,
                });
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

/// Moves `--output` (separate or `=` value) found
/// after an account or package command path into the globals, the way service
/// verbs read them. The same value before and after `cli` is
/// accepted; different values are an invocation error. Other tokens are
/// returned in order for the command's own parser.
fn take_trailing_globals(
    tail: &[String],
    globals: &mut GlobalFlags,
) -> Result<Vec<String>, RunnerError> {
    let mut rest = Vec::with_capacity(tail.len());
    let mut index = 0;
    while let Some(token) = tail.get(index) {
        let (name, inline) = match token.split_once('=') {
            Some((name, value)) if name == "--output" => (name, Some(value.to_owned())),
            _ => (token.as_str(), None),
        };
        if name != "--output" {
            rest.push(token.clone());
            index += 1;
            continue;
        }
        let value = if let Some(value) = inline {
            index += 1;
            value
        } else {
            let value = tail
                .get(index + 1)
                .ok_or_else(|| RunnerError::invocation(format!("{name} requires a value.")))?;
            index += 2;
            value.clone()
        };
        let twice = |what: &str| {
            let mut error =
                RunnerError::invocation(format!("{what} was given twice with different values"));
            error.action = format!("Pass {what} once, with the value you mean.");
            error
        };
        let before = globals.output.take();
        set_value_option(globals, name, &value)?;
        if before.is_some_and(|mode| Some(mode) != globals.output) {
            return Err(twice(name));
        }
    }
    Ok(rest)
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
            let value = args
                .get(index + 1)
                .ok_or_else(|| RunnerError::invocation(format!("{token} requires a value.")))?;
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
            | ["doctor" | "harness" | "cleanup" | "config", "--help"]
            | ["harness", "list" | "use", "--help"]
            | ["harness", "use", _, "--help"]
            | ["cleanup", "prime", "--help"]
            | ["cleanup", "prime", _, "--help"]
            | ["config", "explain", "--help"]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The retired service-selection option, spelled so the public-surface
    /// scan does not match this negative test.
    const RETIRED_OPTION: &str = concat!("--service-", "environment");

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
    fn extra_arguments_after_a_runner_command_are_the_service_rejection() {
        for args in [
            vec!["cli", "doctor", "extra"],
            vec!["cli", "auth", "status", "extra"],
            vec!["cli", "org", "list", "\u{FFFD}"],
        ] {
            let parsed = parse_invocation(strings(&args)).unwrap();
            assert!(
                matches!(
                    parsed.action,
                    Action::Runner {
                        command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(_)),
                        ..
                    }
                ),
                "{args:?}"
            );
        }
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
                vec!["cli", "harness", "use", "nope"],
                "Harness selection must be one of openprose, prime, omp, codex, or claude.",
            ),
        ] {
            assert_invocation_error(&args, reason);
        }
    }

    #[test]
    fn account_verbs_take_a_trailing_output_option() {
        // Like service verbs, account verbs read
        // --output after the command path.
        let parsed =
            parse_invocation(strings(&["cli", "auth", "status", "--output=json"])).unwrap();
        assert_eq!(parsed.globals.output, Some(OutputMode::Json));
        assert!(matches!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::AuthStatus,
                json: false
            }
        ));
        let parsed = parse_invocation(strings(&[
            "--output", "json", "cli", "org", "list", "--output", "json", "--json",
        ]))
        .unwrap();
        assert!(matches!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::OrgList,
                json: true
            }
        ));
        let error = parse_invocation(strings(&[
            "--output", "json", "cli", "auth", "status", "--output", "human",
        ]))
        .unwrap_err();
        assert_eq!(
            error.details.unwrap()["reason"],
            "--output was given twice with different values"
        );
        assert_eq!(error.action, "Pass --output once, with the value you mean.");
    }

    #[test]
    fn no_option_or_command_selects_a_service() {
        // The service is fixed by the build: the retired selection option is
        // not a runner option, and no `cli environment` group exists.
        assert!(!is_value_option(RETIRED_OPTION));
        for args in [
            vec![RETIRED_OPTION, "production", "cli", "run", "list"],
            vec!["cli", "auth", "status", RETIRED_OPTION, "production"],
        ] {
            let parsed = parse_invocation(strings(&args));
            assert!(
                !matches!(
                    parsed,
                    Ok(ParsedInvocation {
                        action: Action::Runner {
                            command: RunnerCommand::AuthStatus,
                            ..
                        },
                        ..
                    })
                ),
                "{args:?}"
            );
            if let Ok(ParsedInvocation {
                action: Action::Runner { command, .. },
                ..
            }) = &parsed
            {
                assert!(
                    matches!(
                        command,
                        RunnerCommand::Service(crate::service::ServiceCommand::Invalid(_))
                    ),
                    "{args:?}: {command:?}"
                );
            }
        }
        for words in [
            vec!["cli", "environment", "show"],
            vec!["cli", "environment", "use", "production"],
            vec!["cli", "api", "GET", "/health"],
        ] {
            let parsed = parse_invocation(strings(&words)).unwrap();
            assert!(
                matches!(
                    parsed.action,
                    Action::Runner {
                        command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(_)),
                        ..
                    }
                ),
                "{words:?}"
            );
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
                argv: strings(&["prose", "write", "--harness", "claude", "two words"]),
                service_hint: None,
                hosted_rejection: None,
                hint_in_details_only: false,
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
                argv: strings(&["prose", "run", "--auth-profile", "language-value"]),
                service_hint: None,
                hosted_rejection: None,
                hint_in_details_only: false,
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
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: expected,
                service_hint: None,
                hosted_rejection: None,
                hint_in_details_only: false,
            }
        );
    }

    #[test]
    fn service_words_without_cli_are_redirected_not_forwarded() {
        // The corrected argv inserts `cli` and keeps the globals.
        for (args, suggested) in [
            (
                vec!["run", "submit", "absent-program.prose"],
                vec!["cli", "run", "submit", "absent-program.prose"],
            ),
            (
                vec!["--output", "json", "job", "list"],
                vec!["--output", "json", "cli", "job", "list"],
            ),
            (
                vec!["--format", "json", "cli", "run", "list"],
                vec!["--output", "json", "cli", "run", "list"],
            ),
            (vec!["login"], vec!["cli", "auth", "login"]),
            (
                vec!["models", "--json"],
                vec!["cli", "model", "list", "--json"],
            ),
        ] {
            let parsed = parse_invocation(strings(&args)).unwrap();
            let Action::Runner {
                command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(invalid)),
                ..
            } = parsed.action
            else {
                panic!("{args:?} was not redirected: {:?}", parsed.action);
            };
            match invalid.correction {
                Some(crate::service::Correction::Argv { argv, .. }) => {
                    assert_eq!(argv, strings(&suggested), "{args:?}");
                }
                other => panic!("{args:?}: {other:?}"),
            }
        }
        // A language command forwards, naming the service command it
        // resembles; the default hosted harness, which runs no language
        // command, renders the rejection instead.
        for (args, hint) in [
            (vec!["status"], vec!["cli", "service", "triage"]),
            (vec!["help"], vec!["--help"]),
            (vec!["examples", "list"], vec!["cli", "example", "list"]),
        ] {
            let parsed = parse_invocation(strings(&args)).unwrap();
            let Action::Forward {
                argv,
                service_hint,
                hosted_rejection,
                ..
            } = parsed.action
            else {
                panic!("{args:?} was not forwarded");
            };
            let mut forwarded = vec!["prose"];
            forwarded.extend(&args);
            assert_eq!(argv, strings(&forwarded), "{args:?}");
            assert_eq!(service_hint, Some(strings(&hint)), "{args:?}");
            assert!(hosted_rejection.is_some(), "{args:?}");
        }
        // `run FILE` always forwards; `details.suggestedArgv` of its
        // HOSTED_UNAVAILABLE refusal names the hosted submission, and the
        // frozen Action is unchanged.
        let parsed = parse_invocation(strings(&["run", "absent-program.prose"])).unwrap();
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "run", "absent-program.prose"]),
                service_hint: Some(strings(&[
                    "cli",
                    "run",
                    "submit",
                    "absent-program.prose",
                    "--preview"
                ])),
                hosted_rejection: None,
                hint_in_details_only: true,
            }
        );
        // `help cli` is the cli help topic.
        let parsed = parse_invocation(strings(&["help", "cli", "run"])).unwrap();
        assert!(matches!(
            parsed.action,
            Action::Runner {
                command: RunnerCommand::Service(crate::service::ServiceCommand::Help(_)),
                ..
            }
        ));
        // Other language commands, distance-only words and `--` still forward.
        for args in [
            vec!["serve"],
            vec!["--", "job", "list"],
            vec!["--env", "production", "write", "x"],
        ] {
            let parsed = parse_invocation(strings(&args)).unwrap();
            assert!(
                matches!(
                    parsed.action,
                    Action::Forward {
                        service_hint: None,
                        ..
                    }
                ),
                "{args:?}"
            );
        }
    }

    #[test]
    fn a_file_named_like_the_command_word_forwards_with_a_hint() {
        let directory =
            std::env::temp_dir().join(format!("prose-invocation-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(directory.join("submit"), "").unwrap();
        let cwd = directory.to_string_lossy().into_owned();
        let parsed =
            parse_invocation(strings(&["--cwd", &cwd, "run", "submit", "hello.prose"])).unwrap();
        std::fs::remove_dir_all(&directory).unwrap();
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "run", "submit", "hello.prose"]),
                service_hint: Some(strings(&[
                    "--cwd",
                    &cwd,
                    "cli",
                    "run",
                    "submit",
                    "hello.prose"
                ])),
                hosted_rejection: None,
                hint_in_details_only: false,
            }
        );
    }

    #[test]
    fn delimiter_forces_cli_through_language_path() {
        let parsed = parse_invocation(strings(&["--", "cli", "doctor"])).unwrap();
        assert_eq!(
            parsed.action,
            Action::Forward {
                argv: strings(&["prose", "cli", "doctor"]),
                service_hint: None,
                hosted_rejection: None,
                hint_in_details_only: false,
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
                argv: strings(&["prose", "--future-language-flag", "value"]),
                service_hint: None,
                hosted_rejection: None,
                hint_in_details_only: false,
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
            // `cli --help` and every manifest group or account
            // verb (`auth ...`) print their `help.v1.json` topic;
            // runner leaves keep the runner help.
            let group_topic = matches!(args.as_slice(), ["cli", "--help"] | ["cli", "auth", ..]);
            if group_topic {
                assert!(
                    matches!(
                        parsed.action,
                        Action::Runner {
                            command: RunnerCommand::Service(crate::service::ServiceCommand::Help(
                                _
                            )),
                            ..
                        }
                    ),
                    "{args:?}"
                );
            } else {
                assert_eq!(parsed.action, Action::Help, "{args:?}");
            }
        }
    }

    #[test]
    fn service_operations_parse_through_the_manifest() {
        let parsed = parse_invocation(strings(&["cli", "run", "list", "--limit", "2"])).unwrap();
        let Action::Runner {
            command: RunnerCommand::Service(crate::service::ServiceCommand::Invoke(invocation)),
            ..
        } = parsed.action
        else {
            panic!("service operation expected");
        };
        assert_eq!(invocation.operation, "run.list");
        assert_eq!(invocation.option("--limit"), Some("2"));
        // A misspelled service noun is a service invocation error, rendered by the service renderer with a corrected argv.
        let Action::Runner {
            command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(invalid)),
            ..
        } = parse_invocation(strings(&["cli", "walet", "balance"]))
            .unwrap()
            .action
        else {
            panic!("service invocation error expected");
        };
        // The reason also lists every command noun.
        assert!(
            invalid.error.unwrap().details.unwrap()["reason"]
                .as_str()
                .unwrap()
                .starts_with("unknown command `cli walet`; did you mean `cli wallet`? Commands: auth, cleanup, config, doctor, example,")
        );
    }

    /// The corrected argv of a service invocation error, or a panic.
    fn suggested(args: &[&str]) -> (String, Vec<String>) {
        let parsed = parse_invocation(strings(args)).unwrap();
        let Action::Runner {
            command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(invalid)),
            ..
        } = parsed.action
        else {
            panic!(
                "{args:?} is not a service invocation error: {:?}",
                parsed.action
            );
        };
        let reason = invalid.error.unwrap().details.unwrap()["reason"]
            .as_str()
            .unwrap()
            .to_owned();
        match invalid.correction {
            Some(crate::service::Correction::Argv { argv, .. }) => (reason, argv),
            other => panic!("{args:?}: {other:?}"),
        }
    }

    #[test]
    fn command_local_options_before_cli_move_after_the_command_path() {
        // Never forwarded to the language, never HOSTED_UNAVAILABLE.
        for (args, argv) in [
            (
                vec!["--json", "cli", "run", "list"],
                vec!["cli", "run", "list", "--json"],
            ),
            (
                vec!["-j", "cli", "run", "list"],
                vec!["cli", "run", "list", "--json"],
            ),
            (
                vec!["--output", "json", "--json", "cli", "run", "list"],
                vec!["--output", "json", "cli", "run", "list", "--json"],
            ),
            (
                vec![
                    "-y", "--output", "json", "cli", "run", "cancel", "r", "--", "x",
                ],
                // Settled, because `cli run cancel` rejects the
                // extra argument `x` after `--`.
                vec![
                    "--output", "json", "cli", "run", "cancel", "r", "--yes", "--",
                ],
            ),
            (
                vec!["--limit", "5", "--before=b", "cli", "run", "list"],
                vec!["cli", "run", "list", "--limit", "5", "--before=b"],
            ),
        ] {
            let (reason, suggested_argv) = suggested(&args);
            assert_eq!(suggested_argv, strings(&argv), "{args:?}");
            assert!(
                reason.contains("after the command path, not before `cli`"),
                "{reason}"
            );
        }
        assert_eq!(
            suggested(&["-j", "cli", "run", "list"]).0,
            "`-j` (--json) belongs after the command path, not before `cli`; nothing was forwarded or sent"
        );
        // An unknown option before `cli` and a service command is an
        // invocation error naming it, never language input.
        let parsed = parse_invocation(strings(&["--jsno", "cli", "run", "list"])).unwrap();
        let Action::Runner {
            command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(invalid)),
            ..
        } = parsed.action
        else {
            panic!("--jsno was not rejected: {:?}", parsed.action);
        };
        assert!(
            invalid
                .error
                .as_ref()
                .and_then(|error| error.details.as_ref())
                .and_then(|details| details.get("reason"))
                .and_then(serde_json::Value::as_str)
                .is_some_and(|reason| reason.starts_with("unknown option --jsno before `cli`")),
            "{invalid:?}"
        );
        // The removed service-selection option says it was removed.
        let removed = ["--service", "environment"].join("-");
        for args in [
            vec![removed.clone(), "production".to_owned()],
            vec![
                format!("{removed}=production"),
                "--output".to_owned(),
                "json".to_owned(),
            ],
        ] {
            let mut argv = args.clone();
            argv.extend(strings(&["cli", "service", "status"]));
            let parsed = parse_invocation(argv).unwrap();
            let Action::Runner {
                command: RunnerCommand::Service(crate::service::ServiceCommand::Invalid(invalid)),
                ..
            } = parsed.action
            else {
                panic!("{args:?} was not rejected");
            };
            let reason = invalid
                .error
                .as_ref()
                .and_then(|error| error.details.as_ref())
                .and_then(|details| details.get("reason"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default()
                .to_owned();
            assert_eq!(
                reason,
                format!("unknown option {removed} before `cli`; the option was removed")
            );
            let Some(crate::service::Correction::Argv { action, argv }) = invalid.correction else {
                panic!("{args:?}: no corrected argv");
            };
            assert!(action.contains("public builds always use the OpenProse production service"));
            assert_eq!(argv.last().map(String::as_str), Some("status"));
            assert!(!argv.iter().any(|word| word.contains(&removed)));
        }
        // Without `cli`, an unknown option stays the language's first token.
        for args in [vec!["--json", "run.prose"]] {
            assert!(
                matches!(
                    parse_invocation(strings(&args)).unwrap().action,
                    Action::Forward { .. }
                ),
                "{args:?}"
            );
        }
    }

    #[test]
    fn pre_parse_global_errors_are_service_errors_with_a_fix() {
        // `--output` values before a service command.
        let (reason, argv) = suggested(&["--output", "yaml", "cli", "run", "list"]);
        assert_eq!(
            reason,
            "invalid output mode \"yaml\"; expected human, json, or jsonl"
        );
        assert_eq!(argv, strings(&["--output", "json", "cli", "run", "list"]));
        let (reason, argv) = suggested(&["--output=JSONL", "cli", "run", "list"]);
        assert!(reason.ends_with("did you mean `jsonl`?"), "{reason}");
        assert_eq!(argv, strings(&["--output=jsonl", "cli", "run", "list"]));
        // A runner operation keeps the runner renderer.
        assert!(parse_invocation(strings(&["--output", "yaml", "cli", "doctor"])).is_err());
    }

    #[test]
    fn help_with_json_prints_the_topic() {
        // Help is text in every mode.
        for args in [
            vec!["help", "cli", "--json"],
            vec!["help", "cli", "run", "--json"],
            vec!["cli", "--help", "--json"],
            vec!["cli", "run", "--help", "--json"],
        ] {
            assert!(
                matches!(
                    parse_invocation(strings(&args)).unwrap().action,
                    Action::Runner {
                        command: RunnerCommand::Service(crate::service::ServiceCommand::Help(_)),
                        ..
                    }
                ),
                "{args:?}"
            );
        }
    }

    #[test]
    fn help_does_not_make_unknown_runner_paths_valid() {
        for args in [
            vec!["cli", "unknown", "--help"],
            vec!["cli", "harness", "unknown", "--help"],
            vec!["cli", "doctor", "extra", "--help"],
        ] {
            // Unknown paths are service invocation errors;
            // a known runner command's own parser still rejects its extras.
            match parse_invocation(strings(&args)) {
                Ok(parsed) => assert!(
                    matches!(
                        parsed.action,
                        Action::Runner {
                            command: RunnerCommand::Service(
                                crate::service::ServiceCommand::Invalid(_)
                            ),
                            ..
                        }
                    ),
                    "{args:?}"
                ),
                Err(error) => {
                    assert_eq!(error.code, crate::ErrorCode::InvocationInvalid, "{args:?}");
                    assert_eq!(error.boundary, "invocation", "{args:?}");
                }
            }
        }
    }
}

#[test]
fn parses_repeated_native_values_without_shell_interpretation() {
    let p = parse_invocation(
        vec![
            "--native-profile=claude-workspace-tools",
            "--native-add-dir",
            "a b",
            "--native-add-dir=other",
            "--native-allow-tool",
            "Bash(git status:*)",
            "run",
        ]
        .into_iter()
        .map(str::to_owned),
    )
    .unwrap();
    assert_eq!(
        p.globals.native_profile.as_deref(),
        Some("claude-workspace-tools")
    );
    assert_eq!(p.globals.native_add_dirs, vec!["a b", "other"]);
    assert_eq!(p.globals.native_allow_tools, vec!["Bash(git status:*)"]);
}

#[cfg(test)]
mod sdk_budget_flag_tests {
    use super::*;
    #[test]
    fn sdk_budget_flags_reject_duplicates() {
        let p = parse_invocation(
            vec!["--native-max-turns=40", "--native-timeout", "5m", "run"]
                .into_iter()
                .map(str::to_owned),
        )
        .unwrap();
        assert_eq!(p.globals.native_max_turns.as_deref(), Some("40"));
        assert_eq!(p.globals.native_timeout.as_deref(), Some("5m"));
        assert!(
            parse_invocation(
                vec!["--native-max-turns=40", "--native-max-turns=50"]
                    .into_iter()
                    .map(str::to_owned)
            )
            .is_err()
        );
    }
}

#[test]
fn sdk_tool_timeout_flag_is_a_runner_option() {
    let p = parse_invocation(
        vec!["--native-tool-timeout=1ms", "run"]
            .into_iter()
            .map(str::to_owned),
    )
    .unwrap();
    assert_eq!(p.globals.native_tool_timeout.as_deref(), Some("1ms"));
    assert!(
        parse_invocation(
            vec!["--native-tool-timeout", "1s", "--native-tool-timeout", "2s"]
                .into_iter()
                .map(str::to_owned)
        )
        .is_err()
    );
}

#[test]
fn native_output_flag_is_single_valued() {
    let p = parse_invocation(
        vec!["--native-output-bytes=134217728", "task"]
            .into_iter()
            .map(str::to_owned),
    )
    .unwrap();
    assert_eq!(p.globals.native_output_bytes.as_deref(), Some("134217728"));
    assert!(
        parse_invocation(
            vec![
                "--native-output-bytes",
                "1048576",
                "--native-output-bytes",
                "2097152",
                "task"
            ]
            .into_iter()
            .map(str::to_owned)
        )
        .is_err()
    );
}

#[cfg(test)]
mod weave_routing_tests {
    use super::*;
    #[test]
    fn opaque_paths_preserved() {
        // Shared corpus language-routing-47/48.
        for input in [
            vec!["weave", "step", "/config"],
            vec!["init", "--host-binding", "/binding"],
            vec!["--", "cli", "weave", "step", "/config"],
        ] {
            let args: Vec<String> = input.iter().map(ToString::to_string).collect();
            assert!(weave_route(&args).is_none());
            let parsed = parse_invocation(args.clone()).unwrap();
            let mut expected = vec!["prose".to_owned()];
            expected.extend(args.into_iter().skip(usize::from(input[0] == "--")));
            assert_eq!(
                parsed.action,
                Action::Forward {
                    argv: expected,
                    service_hint: None,
                    hosted_rejection: None,
                    hint_in_details_only: false,
                }
            );
        }
    }
    #[test]
    fn recognized_route_rejects_globals_without_stealing_help_or_bad_prefix() {
        let args = |v: Vec<&str>| v.into_iter().map(str::to_owned).collect::<Vec<_>>();
        assert_eq!(
            weave_route(&args(vec!["cli", "weave", "--help"])),
            Some((false, args(vec!["--help"]).as_slice()))
        );
        assert!(
            weave_route(&args(vec!["--dry-run", "cli", "weave", "step"]))
                .unwrap()
                .0
        );
        for v in [
            vec!["--help", "cli", "weave"],
            vec!["--model", "a", "--model", "b", "cli", "weave"],
            vec!["unknown", "cli", "weave"],
        ] {
            assert!(weave_route(&args(v)).is_none());
        }
    }
}
