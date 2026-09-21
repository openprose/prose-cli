use serde::Serialize;
use serde_json::{Map, Value};
use std::env;
use std::fmt::{self, Display, Formatter, Write as _};

fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

pub(crate) const NON_COPYABLE_RUNNER_GUIDANCE: &str = "No copyable runner command is available because its path contains control characters; reinstall OpenProse in a path without control characters.";

/// Makes one dynamic value safe to place on a human terminal line without
/// changing the value retained by machine output.
#[must_use]
pub(crate) fn human_safe_scalar(value: &str) -> String {
    human_safe(value, false)
}

/// Makes human prose safe for a terminal while preserving only its intentional
/// LF line boundaries. Literal backslashes are escaped so visible control
/// notation cannot be confused with original harness text.
#[must_use]
pub(crate) fn human_safe_multiline(value: &str) -> String {
    human_safe(value, true)
}

fn human_safe(value: &str, preserve_line_feeds: bool) -> String {
    let mut rendered = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '\\' => rendered.push_str("\\\\"),
            '\u{0008}' => rendered.push_str("\\b"),
            '\t' => rendered.push_str("\\t"),
            '\n' if preserve_line_feeds => rendered.push('\n'),
            '\n' => rendered.push_str("\\n"),
            '\u{000C}' => rendered.push_str("\\f"),
            '\r' => rendered.push_str("\\r"),
            control if control.is_control() || matches!(control, '\u{2028}' | '\u{2029}') => {
                let _ = write!(rendered, "\\u{{{:04X}}}", u32::from(control));
            }
            printable => rendered.push(printable),
        }
    }
    rendered
}

fn has_terminal_line_control(value: &str) -> bool {
    value
        .chars()
        .any(|character| character.is_control() || matches!(character, '\u{2028}' | '\u{2029}'))
}

fn human_runner_executable_for(path: &str) -> String {
    if has_terminal_line_control(path) {
        NON_COPYABLE_RUNNER_GUIDANCE.to_owned()
    } else {
        shell_single_quote(path)
    }
}

fn resolved_human_runner_executable(path: Option<String>) -> String {
    path.map_or_else(
        || NON_COPYABLE_RUNNER_GUIDANCE.to_owned(),
        |path| human_runner_executable_for(&path),
    )
}

/// Returns a copyable reference to the exact running executable without
/// consulting `PATH`. Human guidance may use this value; machine contracts
/// remain deliberately pathless.
#[must_use]
pub(crate) fn human_runner_executable() -> String {
    resolved_human_runner_executable(
        env::current_exe()
            .ok()
            .and_then(|path| path.into_os_string().into_string().ok()),
    )
}

/// Renders one shell-safe command against the exact running executable.
#[must_use]
pub(crate) fn human_runner_command(arguments: &str) -> String {
    let executable = human_runner_executable();
    if executable == NON_COPYABLE_RUNNER_GUIDANCE || has_terminal_line_control(arguments) {
        NON_COPYABLE_RUNNER_GUIDANCE.to_owned()
    } else {
        format!("{executable} {arguments}")
    }
}

fn valid_recovery_handle(handle: &str) -> bool {
    if handle.len() > 160 || !handle.is_ascii() || handle.chars().any(char::is_control) {
        return false;
    }
    let mut parts = handle.split('.');
    let (Some("prime-v1"), Some(directory), Some(token), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return false;
    };
    let Some(suffix) = directory.strip_prefix("openprose-prime-") else {
        return false;
    };
    (6..=64).contains(&suffix.len())
        && suffix
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        && uuid::Uuid::parse_str(token).is_ok()
}

/// Stable, cross-implementation runner error identifiers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorCode {
    ConfigInvalid,
    InvocationInvalid,
    HarnessUnavailable,
    HarnessIncompatible,
    HarnessNeedsAuth,
    TransportUnsupported,
    PromptChannelUnsupported,
    ImageInvalid,
    ImageTooLarge,
    RecursiveInvocation,
    StartupTimeout,
    ProtocolMalformed,
    ProtocolTruncated,
    HarnessFailed,
    SemanticStatusUnknown,
    Cancelled,
    ProcessCleanupFailed,
    ServiceUnavailable,
    ServiceAuthRequired,
    ServiceProtocolInvalid,
    CredentialStoreUnavailable,
    DeviceAuthFailed,
    DeviceAuthExpired,
    HostedUnavailable,
    HostedAuthRequired,
    HostedQuotaExceeded,
    #[serde(rename = "INTERNAL_ERROR")]
    InternalRunnerFault,
}

impl ErrorCode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ConfigInvalid => "CONFIG_INVALID",
            Self::InvocationInvalid => "INVOCATION_INVALID",
            Self::HarnessUnavailable => "HARNESS_UNAVAILABLE",
            Self::HarnessIncompatible => "HARNESS_INCOMPATIBLE",
            Self::HarnessNeedsAuth => "HARNESS_NEEDS_AUTH",
            Self::TransportUnsupported => "TRANSPORT_UNSUPPORTED",
            Self::PromptChannelUnsupported => "PROMPT_CHANNEL_UNSUPPORTED",
            Self::ImageInvalid => "IMAGE_INVALID",
            Self::ImageTooLarge => "IMAGE_TOO_LARGE",
            Self::RecursiveInvocation => "RECURSIVE_INVOCATION",
            Self::StartupTimeout => "STARTUP_TIMEOUT",
            Self::ProtocolMalformed => "PROTOCOL_MALFORMED",
            Self::ProtocolTruncated => "PROTOCOL_TRUNCATED",
            Self::HarnessFailed => "HARNESS_FAILED",
            Self::SemanticStatusUnknown => "SEMANTIC_STATUS_UNKNOWN",
            Self::Cancelled => "CANCELLED",
            Self::ProcessCleanupFailed => "PROCESS_CLEANUP_FAILED",
            Self::ServiceUnavailable => "SERVICE_UNAVAILABLE",
            Self::ServiceAuthRequired => "SERVICE_AUTH_REQUIRED",
            Self::ServiceProtocolInvalid => "SERVICE_PROTOCOL_INVALID",
            Self::CredentialStoreUnavailable => "CREDENTIAL_STORE_UNAVAILABLE",
            Self::DeviceAuthFailed => "DEVICE_AUTH_FAILED",
            Self::DeviceAuthExpired => "DEVICE_AUTH_EXPIRED",
            Self::HostedUnavailable => "HOSTED_UNAVAILABLE",
            Self::HostedAuthRequired => "HOSTED_AUTH_REQUIRED",
            Self::HostedQuotaExceeded => "HOSTED_QUOTA_EXCEEDED",
            Self::InternalRunnerFault => "INTERNAL_ERROR",
        }
    }

    #[must_use]
    pub const fn exit_code(self) -> u8 {
        match self {
            Self::ConfigInvalid | Self::InvocationInvalid => 2,
            Self::HarnessUnavailable
            | Self::HarnessIncompatible
            | Self::HarnessNeedsAuth
            | Self::ServiceUnavailable
            | Self::ServiceAuthRequired
            | Self::ServiceProtocolInvalid
            | Self::CredentialStoreUnavailable
            | Self::DeviceAuthFailed
            | Self::DeviceAuthExpired
            | Self::HostedUnavailable
            | Self::HostedAuthRequired
            | Self::HostedQuotaExceeded => 10,
            Self::TransportUnsupported
            | Self::PromptChannelUnsupported
            | Self::ImageInvalid
            | Self::ImageTooLarge
            | Self::RecursiveInvocation => 20,
            Self::StartupTimeout => 21,
            Self::ProtocolMalformed | Self::ProtocolTruncated | Self::HarnessFailed => 22,
            Self::SemanticStatusUnknown => 23,
            Self::Cancelled => 24,
            Self::ProcessCleanupFailed => 25,
            Self::InternalRunnerFault => 70,
        }
    }
}

impl Display for ErrorCode {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A closed, actionable runner failure. `message` and `action` must never
/// contain credentials or raw protocol payloads.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RunnerError {
    pub schema: &'static str,
    pub code: ErrorCode,
    pub boundary: String,
    pub message: String,
    pub action: String,
    pub exit_code: u8,
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Box<Map<String, Value>>>,
}

impl RunnerError {
    #[must_use]
    pub fn new(
        code: ErrorCode,
        boundary: impl Into<String>,
        message: impl Into<String>,
        action: impl Into<String>,
    ) -> Self {
        Self {
            schema: "openprose.runner-error/1",
            code,
            boundary: boundary.into(),
            message: message.into(),
            action: action.into(),
            exit_code: code.exit_code(),
            retryable: matches!(
                code,
                ErrorCode::StartupTimeout
                    | ErrorCode::HarnessFailed
                    | ErrorCode::HostedUnavailable
                    | ErrorCode::HostedQuotaExceeded
            ),
            details: None,
        }
    }

    /// Constructs the exact v1 taxonomy entry for `code`.
    #[must_use]
    pub fn catalog(code: ErrorCode) -> Self {
        let (boundary, message, action, retryable) = match code {
            ErrorCode::ServiceUnavailable => ("hosted-service","The staging account service is unavailable.","Retry the staging account command later.",true),
            ErrorCode::ServiceAuthRequired => ("authentication","Staging account authentication is required.","Run prose --service-environment staging cli auth login, then retry.",false),
            ErrorCode::ServiceProtocolInvalid => ("protocol","The staging account service returned an invalid response.","Retry later and report the sanitized error code if it persists.",false),
            ErrorCode::CredentialStoreUnavailable => ("authentication","The operating system credential store is unavailable.","Unlock or configure the operating system credential store, then retry.",false),
            ErrorCode::DeviceAuthFailed => ("authentication","Device authorization failed.","Run the staging login command again and authorize the displayed code.",false),
            ErrorCode::DeviceAuthExpired => ("authentication","Device authorization expired.","Run the staging login command again to obtain a new code.",true),

            ErrorCode::ConfigInvalid => (
                "configuration",
                "Runner configuration is invalid.",
                "Correct or remove the reported configuration source or setting, then invoke the `cli config explain` runner operation to verify the repair.",
                false,
            ),
            ErrorCode::InvocationInvalid => (
                "invocation",
                "Runner invocation is invalid.",
                "Review the runner syntax with the --help option, place global options before cli, and retry the command.",
                false,
            ),
            ErrorCode::HarnessUnavailable => (
                "adapter",
                "The selected harness executable is unavailable.",
                "Install the selected harness and ensure its executable is on PATH, then invoke the `cli doctor` runner operation.",
                false,
            ),
            ErrorCode::HarnessIncompatible => (
                "adapter",
                "The selected harness version is incompatible with this adapter.",
                "Run the exact Repair command reported with this error, then retry.",
                false,
            ),
            ErrorCode::HarnessNeedsAuth => (
                "authentication",
                "The selected harness is not authenticated.",
                "Sign in with the selected harness. For Codex, run `codex login`; for Claude, run `claude auth login`; for Prime or OMP cached login, select its explicit harness-login `--auth-profile`; for API-key routes, configure the selected profile. Then retry.",
                false,
            ),
            ErrorCode::TransportUnsupported => (
                "adapter",
                "The selected harness does not support the requested transport.",
                "Choose a transport listed for this harness by the `cli harness list` runner operation.",
                false,
            ),
            ErrorCode::PromptChannelUnsupported => (
                "adapter",
                "The selected adapter cannot use a manifest-permitted instruction placement.",
                "Choose a strict adapter reported by the `cli harness list` runner operation.",
                false,
            ),
            ErrorCode::ImageInvalid => (
                "image",
                "The Skill Runtime Image failed structural or digest verification.",
                "Reinstall this OpenProse CLI artifact from a verified release.",
                false,
            ),
            ErrorCode::ImageTooLarge => (
                "image",
                "The Skill Runtime Image exceeds the selected transport limit.",
                "Choose an adapter whose reported image limit accepts this image version.",
                false,
            ),
            ErrorCode::RecursiveInvocation => (
                "invocation",
                "A prose wrapper invocation attempted to enter itself recursively.",
                "Run the harness directly for the interactive skill path, or select a harness executable that is not this wrapper.",
                false,
            ),
            ErrorCode::StartupTimeout => (
                "process",
                "The selected harness did not start before the startup deadline.",
                "Check the harness with the `cli doctor` runner operation and retry with an appropriate `--timeout`.",
                true,
            ),
            ErrorCode::ProtocolMalformed => (
                "protocol",
                "The harness emitted a malformed or out-of-order structured record.",
                "Invoke the `cli doctor` runner operation and install a harness version supported by the selected adapter.",
                false,
            ),
            ErrorCode::ProtocolTruncated => (
                "protocol",
                "The harness stream ended before its required terminal record.",
                "Retry once; if the problem persists, invoke the `cli doctor` runner operation and inspect sanitized diagnostics.",
                true,
            ),
            ErrorCode::HarnessFailed => (
                "process",
                "The harness failed before a valid semantic terminal result was accepted.",
                "Inspect the harness diagnostic on stderr and invoke the `cli doctor` runner operation.",
                false,
            ),
            ErrorCode::SemanticStatusUnknown => (
                "semantic-terminal",
                "Transport completed without a trustworthy OpenProse semantic status.",
                "Choose a strict adapter that carries the image's terminal envelope.",
                false,
            ),
            ErrorCode::Cancelled => (
                "process",
                "The runner was cancelled before semantic completion.",
                "Retry the command when you are ready to let it complete.",
                true,
            ),
            ErrorCode::ProcessCleanupFailed => (
                "cleanup",
                "The runner could not verify cleanup of its direct child, original process group, or owned harness service.",
                "Inspect only the reported child, process group, or owned harness service; do not stop unrelated services. Then invoke the `cli doctor` runner operation before retrying.",
                false,
            ),
            ErrorCode::HostedUnavailable => (
                "hosted-service",
                "OpenProse-billed execution is not available in this build.",
                "Select an available BYO harness with the `cli harness use <id>` runner operation, then invoke the `cli doctor` runner operation.",
                false,
            ),
            ErrorCode::HostedAuthRequired => (
                "authentication",
                "An OpenProse account is required for OpenProse-billed execution.",
                "Invoke the `cli auth login` runner operation, then retry.",
                false,
            ),
            ErrorCode::HostedQuotaExceeded => (
                "hosted-service",
                "The OpenProse account has no available execution quota.",
                "Review OpenProse billing or quota status with the `cli auth status` runner operation.",
                false,
            ),
            ErrorCode::InternalRunnerFault => (
                "runner",
                "The runner encountered an internal fault.",
                "Retry with `--verbose` and report the sanitized diagnostic identifier.",
                false,
            ),
        };
        let mut error = Self::new(code, boundary, message, action);
        error.retryable = retryable;
        error
    }

    #[must_use]
    pub fn with_detail(mut self, key: &str, value: impl Into<Value>) -> Self {
        self.details
            .get_or_insert_with(|| Box::new(Map::new()))
            .insert(key.to_owned(), value.into());
        self
    }

    #[must_use]
    pub fn config(message: impl Into<String>) -> Self {
        Self::catalog(ErrorCode::ConfigInvalid).with_detail("reason", message.into())
    }

    /// Constructs an invocation failure for malformed runner-owned command
    /// syntax. Persisted and environment configuration errors continue to use
    /// the separate shared taxonomy entry through [`Self::config`].
    #[must_use]
    pub fn invocation(message: impl Into<String>) -> Self {
        Self::catalog(ErrorCode::InvocationInvalid).with_detail("reason", message.into())
    }

    #[must_use]
    pub fn human_version_repair_details(&self) -> Vec<String> {
        if !matches!(
            self.code,
            ErrorCode::HarnessUnavailable | ErrorCode::HarnessIncompatible
        ) {
            return Vec::new();
        }
        let Some(details) = self.details.as_ref() else {
            return Vec::new();
        };
        let mut lines = Vec::new();
        if let Some(runtime) = details
            .get("runtimePrerequisite")
            .and_then(Value::as_object)
            .filter(|runtime| {
                runtime.get("runtime").and_then(Value::as_str) == Some("bun")
                    && runtime.get("versionRange").and_then(Value::as_str) == Some(">=1.3.14")
                    && runtime
                        .get("detectedVersion")
                        .is_some_and(|value| value.is_null() || value.as_str().is_some())
                    && runtime.get("repairCommand").and_then(Value::as_str)
                        == Some("npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9")
            })
        {
            lines.push("Runtime prerequisite: bun".to_owned());
            lines.push(format!(
                "Detected runtime version: {}",
                human_safe_scalar(
                    runtime
                        .get("detectedVersion")
                        .and_then(Value::as_str)
                        .unwrap_or("missing")
                )
            ));
            lines.push("Required runtime version: >=1.3.14".to_owned());
            lines.push(format!(
                "Repair: {}",
                human_safe_scalar(
                    runtime
                        .get("repairCommand")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                )
            ));
        }
        if let Some(detected) = details.get("detectedVersion").and_then(Value::as_str) {
            lines.push(format!("Detected version: {}", human_safe_scalar(detected)));
        }
        if let Some(admitted) = details.get("admittedVersions").and_then(Value::as_array) {
            let versions = admitted
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>();
            if versions.len() == admitted.len() {
                lines.push(format!(
                    "Admitted versions: {}",
                    versions
                        .iter()
                        .map(|version| human_safe_scalar(version))
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            }
        }
        if let Some(repair) = details.get("repairCommand").and_then(Value::as_str) {
            lines.push(format!("Repair: {}", human_safe_scalar(repair)));
        }
        lines
    }

    #[must_use]
    pub fn human_action(&self) -> String {
        let executable = human_runner_executable();
        let action = human_safe_scalar(&self.action);
        if executable == NON_COPYABLE_RUNNER_GUIDANCE {
            format!("{NON_COPYABLE_RUNNER_GUIDANCE} {action}")
        } else {
            format!("Use the exact executable at {executable} for runner operations. {action}")
        }
    }
}

impl Display for RunnerError {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} at {}: {}",
            self.code,
            human_safe_scalar(&self.boundary),
            human_safe_scalar(&self.message)
        )?;
        let source = self
            .details
            .as_ref()
            .and_then(|details| details.get("source"))
            .and_then(Value::as_str);
        if let Some(source) = source {
            write!(f, "\nSource: {}", human_safe_scalar(source))?;
        } else if self.boundary == "configuration" {
            write!(f, "\nSource: unavailable")?;
        }
        let reason = self
            .details
            .as_ref()
            .and_then(|details| details.get("reason"))
            .and_then(Value::as_str);
        if let Some(reason) = reason {
            write!(f, "\nDetail: {}", human_safe_scalar(reason))?;
        } else if self.boundary == "configuration" {
            write!(f, "\nDetail: unavailable")?;
        }
        for detail in self.human_version_repair_details() {
            write!(f, "\n{detail}")?;
        }
        write!(f, "\nAction: {}", self.human_action())?;
        if let Some(arguments) = self
            .details
            .as_ref()
            .and_then(|details| details.get("cleanupArgv"))
            .and_then(Value::as_array)
            .filter(|values| {
                values.len() == 4
                    && values[0].as_str() == Some("cli")
                    && values[1].as_str() == Some("cleanup")
                    && values[2].as_str() == Some("prime")
                    && values[3].as_str().is_some_and(valid_recovery_handle)
            })
        {
            write!(
                f,
                "\nRecovery: preserve the original temporary-root environment, then run: {}",
                human_runner_command(&format!(
                    "cli cleanup prime {}",
                    arguments[3].as_str().expect("validated above")
                ))
            )?;
        }
        Ok(())
    }
}

impl std::error::Error for RunnerError {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn human_safe_scalar_matches_the_shared_hostile_fixture() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/human/human-safe-scalars.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            assert_eq!(
                human_safe_scalar(case["input"].as_str().unwrap()),
                case["rendered"].as_str().unwrap(),
                "{}",
                case["id"]
            );
        }
        for range in fixture["unsafeCodePointRanges"].as_array().unwrap() {
            let start = u32::try_from(range["start"].as_u64().unwrap()).unwrap();
            let end = u32::try_from(range["end"].as_u64().unwrap()).unwrap();
            for code_point in start..=end {
                let character = char::from_u32(code_point).unwrap();
                let expected = match character {
                    '\u{0008}' => "\\b".to_owned(),
                    '\t' => "\\t".to_owned(),
                    '\n' => "\\n".to_owned(),
                    '\u{000C}' => "\\f".to_owned(),
                    '\r' => "\\r".to_owned(),
                    _ => format!("\\u{{{code_point:04X}}}"),
                };
                assert_eq!(human_safe_scalar(&character.to_string()), expected);
            }
        }
        for case in fixture["multilineCases"].as_array().unwrap() {
            assert_eq!(
                human_safe_multiline(case["input"].as_str().unwrap()),
                case["rendered"].as_str().unwrap(),
                "{}",
                case["id"]
            );
        }
    }

    #[test]
    fn hostile_human_error_details_are_visible_without_terminal_or_label_injection() {
        let source = "/tmp/config\nAction: forged\t\u{001B}[31m.toml:7";
        let reason = "invalid setting\r\nSource: forged\u{0008}\u{0085}";
        let error = RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("source", source)
            .with_detail("reason", reason);
        let machine = serde_json::to_value(&error).unwrap();
        let rendered = error.to_string();
        assert!(rendered.contains("Source: /tmp/config\\nAction: forged\\t\\u{001B}[31m.toml:7\n"));
        assert!(rendered.contains("Detail: invalid setting\\r\\nSource: forged\\b\\u{0085}\n"));
        assert_eq!(
            rendered
                .lines()
                .filter(|line| line.starts_with("Action:"))
                .count(),
            1
        );
        assert_eq!(
            rendered
                .lines()
                .filter(|line| line.starts_with("Source:"))
                .count(),
            1
        );
        assert!(!rendered.contains('\u{001B}'));
        assert!(!rendered.contains('\t'));
        assert_eq!(machine["details"]["source"], source);
        assert_eq!(machine["details"]["reason"], reason);
    }

    #[test]
    fn unsafe_runner_paths_and_recovery_handles_never_become_copyable_commands() {
        assert_eq!(
            human_runner_executable_for("/tmp/prose\nAction: forged"),
            NON_COPYABLE_RUNNER_GUIDANCE
        );
        let error = RunnerError::catalog(ErrorCode::ProcessCleanupFailed).with_detail(
            "cleanupArgv",
            json!([
                "cli",
                "cleanup",
                "prime",
                "prime-v1.openprose-prime-Fixture1.bad\nhandle"
            ]),
        );
        let rendered = error.to_string();
        assert!(!rendered.contains("Recovery:"));
        assert!(!rendered.contains("bad\nhandle"));
    }

    #[test]
    fn an_unresolved_runner_path_is_not_advertised_as_a_copyable_command() {
        let rendered = resolved_human_runner_executable(None);
        assert_eq!(rendered, NON_COPYABLE_RUNNER_GUIDANCE);
        assert!(!rendered.contains("<this-exact-prose-executable>"));
    }

    #[test]
    fn hostile_detected_version_is_one_terminal_safe_line_without_machine_mutation() {
        let detected = "codex-cli 0.0.0\nAction: forged\u{001B}[31m";
        let error = RunnerError::catalog(ErrorCode::HarnessIncompatible)
            .with_detail("detectedVersion", detected)
            .with_detail("admittedVersions", ["0.149.0-alpha.4.1"])
            .with_detail(
                "repairCommand",
                "npm install --global @openai/codex@0.149.0-alpha.4.1",
            );
        let rendered = error.to_string();
        assert!(
            rendered.contains("Detected version: codex-cli 0.0.0\\nAction: forged\\u{001B}[31m")
        );
        assert_eq!(
            rendered
                .lines()
                .filter(|line| line.starts_with("Action:"))
                .count(),
            1
        );
        assert_eq!(
            serde_json::to_value(error).unwrap()["details"]["detectedVersion"],
            detected
        );
    }

    #[test]
    fn invocation_failure_uses_its_distinct_frozen_code_and_action() {
        let error = RunnerError::invocation("runner option --timeout requires a value");
        assert_eq!(error.code, ErrorCode::InvocationInvalid);
        assert_eq!(error.boundary, "invocation");
        assert_eq!(error.message, "Runner invocation is invalid.");
        assert_eq!(
            error.action,
            "Review the runner syntax with the --help option, place global options before cli, and retry the command."
        );
        assert_eq!(error.exit_code, 2);
        assert!(!error.retryable);
        assert_eq!(
            error.details.as_deref(),
            Some(&Map::from_iter([(
                "reason".to_owned(),
                Value::String("runner option --timeout requires a value".to_owned()),
            )]))
        );

        let configuration = RunnerError::config("invalid saved value");
        assert_eq!(configuration.boundary, "configuration");
        assert_eq!(configuration.message, "Runner configuration is invalid.");
        assert_eq!(
            configuration.action,
            "Correct or remove the reported configuration source or setting, then invoke the `cli config explain` runner operation to verify the repair."
        );
    }

    #[test]
    fn owned_service_failure_renders_its_opaque_recovery_command() {
        let handle = "prime-v1.openprose-prime-Fixture1.018f47a6-7d2c-7b10-8a2e-1a2b3c4d5e6f";
        let error = RunnerError::catalog(ErrorCode::ProcessCleanupFailed)
            .with_detail("cleanupHandle", handle)
            .with_detail("cleanupArgv", json!(["cli", "cleanup", "prime", handle]));
        let rendered = error.to_string();
        assert!(rendered.contains(&human_runner_command(&format!(
            "cli cleanup prime {handle}"
        ))));
        assert!(!rendered.contains("$PROSE"));
        assert!(!rendered.contains("Recovery: prose cli cleanup"));
        assert!(!rendered.contains("`prose cli"));
        assert!(!rendered.contains("/tmp/"));
    }

    #[test]
    fn catalog_matches_the_frozen_shared_taxonomy() {
        let taxonomy: Value =
            serde_json::from_str(include_str!("../../../../shared/errors/taxonomy.v1.json"))
                .unwrap();
        let codes = [
            ErrorCode::ConfigInvalid,
            ErrorCode::InvocationInvalid,
            ErrorCode::HarnessUnavailable,
            ErrorCode::HarnessIncompatible,
            ErrorCode::HarnessNeedsAuth,
            ErrorCode::TransportUnsupported,
            ErrorCode::PromptChannelUnsupported,
            ErrorCode::ImageInvalid,
            ErrorCode::ImageTooLarge,
            ErrorCode::RecursiveInvocation,
            ErrorCode::StartupTimeout,
            ErrorCode::ProtocolMalformed,
            ErrorCode::ProtocolTruncated,
            ErrorCode::HarnessFailed,
            ErrorCode::SemanticStatusUnknown,
            ErrorCode::Cancelled,
            ErrorCode::ProcessCleanupFailed,
            ErrorCode::HostedUnavailable,
            ErrorCode::HostedAuthRequired,
            ErrorCode::HostedQuotaExceeded,
            ErrorCode::ServiceUnavailable,
            ErrorCode::ServiceAuthRequired,
            ErrorCode::ServiceProtocolInvalid,
            ErrorCode::CredentialStoreUnavailable,
            ErrorCode::DeviceAuthFailed,
            ErrorCode::DeviceAuthExpired,
            ErrorCode::InternalRunnerFault,
        ];
        let expected = taxonomy["errors"].as_array().unwrap();
        assert_eq!(expected.len(), codes.len());
        for code in codes {
            let actual = serde_json::to_value(RunnerError::catalog(code)).unwrap();
            let expected = expected
                .iter()
                .find(|entry| entry["code"] == code.as_str())
                .unwrap();
            for field in [
                "code",
                "boundary",
                "message",
                "action",
                "exitCode",
                "retryable",
            ] {
                assert_eq!(actual[field], expected[field], "{code} field {field}");
            }
        }
    }

    #[test]
    fn harness_auth_catalog_uses_the_admitted_claude_login_command() {
        let error = RunnerError::catalog(ErrorCode::HarnessNeedsAuth);
        assert!(error.action.contains("for Claude, run `claude auth login`"));
        assert!(!error.action.contains("enter `/login`"));
        assert_eq!(
            serde_json::to_value(&error).unwrap()["action"],
            error.action
        );
    }
}
