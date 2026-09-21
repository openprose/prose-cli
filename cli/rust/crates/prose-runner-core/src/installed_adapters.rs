//! Mechanical descriptions of the four installed-process baseline adapters.
//!
//! These adapters transport an opaque Skill Runtime Image and task envelope.
//! They deliberately contain no `OpenProse` command or program semantics.

mod native_tools;
mod claude_shutdown;

use crate::image::{sha256_hex, RuntimeImage};
use crate::{ErrorCode, RunnerError};
use prose_process_supervisor::{
    CancellationToken, CommandProbe, CommandProbeOutcome, EnvironmentPolicy, JsonlProtocol,
    PrivatePromptFiles, PrivatePromptFilesCreateError, ProcessSpec, Sensitivity, StdinLifecycle,
    StreamLimits, VersionProbe, VersionProbeOutput,
};
use serde::de::{Error as _, MapAccess, SeqAccess, Visitor};
use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{json, Value};
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

const MAX_PRIME_INLINE_IMAGE_BYTES: usize = 128 * 1024;
const MAX_ARGV_ITEMS: usize = 64;
const MAX_POSIX_ARGUMENT_BYTES: usize = 131_071;
const MAX_POSIX_ARGV_BYTES: usize = 262_144;
const MAX_WINDOWS_ARGUMENT_UTF16_UNITS: usize = 8_191;
const MAX_WINDOWS_ARGV_CHARGED_UTF16_UNITS: usize = 30_000;
const PRIME_RPC_COLLECTION_LIMIT: usize = 4_096;
const PRIME_RPC_STRING_BYTES_LIMIT: usize = 65_536;
const PRIME_RPC_NESTING_LIMIT: usize = 32;
const OMP_TERMINAL_COLLECTION_LIMIT: usize = 4_096;
const OMP_TERMINAL_STRING_BYTES_LIMIT: usize = 65_536;
const MAX_BUN_SEMVER_COMPONENT: u64 = 1_000_000;
const OMP_COUNTER_FIELDS: &[&str] = &[
    "total", "ok", "error", "skipped", "blocked", "timeout", "aborted",
];
const OMP_AGENT_END_FIELDS: &[&str] = &[
    "type",
    "messages",
    "isTerminal",
    "messageCount",
    "telemetry",
    "coverage",
];
const OMP_ASSISTANT_MESSAGE_EVENTS: &[&str] = &[
    "text_start",
    "text_delta",
    "text_end",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
];
const PRIME_AGENT_TELEMETRY_CONTROL: (&str, &str) = ("PRIME_AGENT_TELEMETRY", "0");
const BASE_ENVIRONMENT: &[&str] = &[
    "PATH",
    "HOME",
    "USER",
    "USERPROFILE",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "SSH_AUTH_SOCK",
    "__CF_USER_TEXT_ENCODING",
];
// Deliberately excludes HOME, user/profile roots, XDG roots, credential-store
// overrides, SSH agent sockets, and provider credentials.  An executable has
// not been admitted when this environment is used, so it receives only the
// process/locale values needed to answer `--version`.
const VERSION_PROBE_ENVIRONMENT: &[&str] = &[
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "TMP",
    "TEMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstalledAdapter {
    AgentsSdkJsonl,
    CodexExecJson,
    ClaudePrintStreamJson,
    PrimeRpc,
    OmpRpc,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimePrerequisiteRequirement {
    pub runtime: &'static str,
    pub version_range: &'static str,
    pub repair_command: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimePrerequisiteObservation {
    pub runtime: &'static str,
    pub version_range: &'static str,
    pub detected_version: Option<String>,
    pub availability: &'static str,
    pub repair_command: &'static str,
}

impl InstalledAdapter {
    #[must_use]
    pub const fn harness(self) -> &'static str {
        match self {
            Self::AgentsSdkJsonl => "agents-sdk",
            Self::CodexExecJson => "codex",
            Self::ClaudePrintStreamJson => "claude",
            Self::PrimeRpc => "prime",
            Self::OmpRpc => "omp",
        }
    }

    #[must_use]
    pub const fn transport(self) -> &'static str {
        match self {
            Self::AgentsSdkJsonl => "jsonl",
            Self::CodexExecJson => "exec-json",
            Self::ClaudePrintStreamJson => "print-stream-json",
            Self::PrimeRpc | Self::OmpRpc => "rpc",
        }
    }

    #[must_use]
    pub const fn id(self) -> &'static str {
        match self {
            Self::AgentsSdkJsonl => "agents-sdk/jsonl",
            Self::CodexExecJson => "codex/exec-json",
            Self::ClaudePrintStreamJson => "claude/print-stream-json",
            Self::PrimeRpc => "prime/rpc",
            Self::OmpRpc => "omp/rpc",
        }
    }

    #[must_use]
    pub const fn executable_names(self) -> &'static [&'static str] {
        match self {
            Self::AgentsSdkJsonl => &["prose-agents-sdk"],
            Self::CodexExecJson => &["codex"],
            Self::ClaudePrintStreamJson => &["claude"],
            Self::PrimeRpc => &["prime-agent"],
            Self::OmpRpc => &["omp"],
        }
    }

    #[must_use]
    pub const fn default_probe_auth_group(self) -> &'static str {
        match self {
            Self::AgentsSdkJsonl => "openai-api-key",
            Self::CodexExecJson => "cached-chatgpt-login",
            Self::ClaudePrintStreamJson => "claude-subscription",
            Self::PrimeRpc => "prime-harness-login",
            Self::OmpRpc => "omp-harness-login",
        }
    }

    #[must_use]
    pub const fn auth_profiles(self) -> &'static [&'static str] {
        match self {
            Self::AgentsSdkJsonl => &["openai-api-key"],
            Self::CodexExecJson => &[
                "cached-chatgpt-login",
                "openai-api-key",
                "codex-access-token",
            ],
            Self::ClaudePrintStreamJson => &["claude-subscription", "anthropic-api-key"],
            Self::PrimeRpc => &[
                "prime-harness-login",
                "anthropic",
                "openai",
                "openrouter",
                "google",
                "github-copilot",
                "aws-bedrock",
            ],
            Self::OmpRpc => &[
                "omp-harness-login",
                "anthropic",
                "openai",
                "openrouter",
                "google",
                "github-copilot",
                "aws-bedrock",
            ],
        }
    }

    #[must_use]
    pub const fn prompt_placement(self) -> &'static str {
        match self {
            Self::CodexExecJson => if cfg!(any(test, feature = "test-seams")) { "user-prefix-framed" } else { "developer" },
            Self::AgentsSdkJsonl | Self::ClaudePrintStreamJson | Self::PrimeRpc | Self::OmpRpc => "system-append",
        }
    }

    #[must_use]
    pub const fn prompt_strictness(self) -> &'static str {
        match self {
            Self::CodexExecJson => if cfg!(any(test, feature = "test-seams")) { "degraded" } else { "strict" },
            Self::AgentsSdkJsonl | Self::ClaudePrintStreamJson | Self::PrimeRpc | Self::OmpRpc => "strict",
        }
    }

    #[must_use]
    pub const fn isolation_guarantee(self) -> &'static str {
        match self {
            Self::CodexExecJson | Self::OmpRpc => "unsupported",
            Self::AgentsSdkJsonl | Self::ClaudePrintStreamJson | Self::PrimeRpc => "advisory",
        }
    }

    #[must_use]
    pub fn protocol(self) -> JsonlProtocol {
        match self {
            Self::AgentsSdkJsonl => JsonlProtocol::installed("start","final",["tool_call","tool_result"]).with_failure_events(["error"]),
            Self::CodexExecJson => JsonlProtocol::installed(
                "thread.started",
                "turn.completed",
                [
                    "turn.started",
                    "item.started",
                    "item.updated",
                    "item.completed",
                ],
            )
            .with_failure_events(["turn.failed", "error"]),
            Self::ClaudePrintStreamJson => {
                JsonlProtocol::installed("system", "result", ["system", "assistant", "user", "stream_event", "tool_progress"])
            }
            Self::PrimeRpc => JsonlProtocol::installed(
                "response",
                "agent_end",
                [
                    "agent_start",
                    "turn_start",
                    "message_start",
                    "message_update",
                    "message_end",
                    "turn_end",
                    "tool_execution_start",
                    "tool_execution_update",
                    "tool_execution_end",
                    "rlm_child_update",
                    "session_action_update",
                ],
            )
            .with_failure_events(["extension_ui_request"]),
            Self::OmpRpc => JsonlProtocol::installed(
                "ready",
                "agent_end",
                [
                    "available_commands_update",
                    "response",
                    "agent_start",
                    "turn_start",
                    "message_start",
                    "message_update",
                    "message_end",
                    "turn_end",
                    "extension_ui_request",
                    "tool_execution_start",
                    "tool_execution_update",
                    "tool_execution_end",
                ],
            )
            .with_allowed_after_terminal_events(["response", "extension_ui_request"]),
        }
    }

    #[must_use]
    pub fn version_probe(self) -> VersionProbe {
        VersionProbe {
            argv: vec!["--version".into()],
            timeout: Duration::from_secs(5),
            max_output_bytes: 4096,
            required_substring: None,
            output: self.recipe_version_stream(),
        }
    }

    fn recipe_version_stream(self) -> VersionProbeOutput {
        let recipe: Value = serde_json::from_str(self.recipe_json())
            .expect("embedded adapter recipe must be valid JSON");
        match recipe
            .pointer("/probe/versionStream")
            .and_then(Value::as_str)
        {
            Some("stdout") => VersionProbeOutput::Stdout,
            Some("stderr") => VersionProbeOutput::Stderr,
            _ => panic!(
                "embedded adapter recipe {} must declare probe.versionStream",
                self.id()
            ),
        }
    }

    fn recipe_stdin_lifecycle(self) -> StdinLifecycle {
        let recipe: Value = serde_json::from_str(self.recipe_json())
            .expect("embedded adapter recipe must be valid JSON");
        match recipe
            .pointer("/launch/stdinLifecycle")
            .and_then(Value::as_str)
        {
            Some("close-after-write") => StdinLifecycle::CloseAfterWrite,
            Some("close-after-terminal-event") => StdinLifecycle::CloseAfterTerminalEvent,
            _ => panic!(
                "embedded adapter recipe {} must declare launch.stdinLifecycle",
                self.id()
            ),
        }
    }

    #[must_use]
    pub fn auth_probe(self) -> Option<CommandProbe> {
        let argv = match self {
            Self::CodexExecJson => Some(vec!["login".into(), "status".into()]),
            Self::ClaudePrintStreamJson => {
                Some(vec!["auth".into(), "status".into(), "--json".into()])
            }
            Self::AgentsSdkJsonl | Self::PrimeRpc | Self::OmpRpc => None,
        }?;
        Some(CommandProbe {
            argv,
            timeout: Duration::from_secs(5),
            max_output_bytes: 64 * 1024,
        })
    }

    /// Classifies only evidence the harness readiness command actually
    /// establishes. Prime and OMP do not expose an auth probe: actual execution
    /// is their first authentication authority.
    ///
    /// # Errors
    ///
    /// Returns `HARNESS_NEEDS_AUTH` when the harness reports that its local
    /// authentication state is not usable.
    pub fn classify_auth_probe(
        self,
        outcome: &CommandProbeOutcome,
    ) -> Result<&'static str, RunnerError> {
        let combined = format!("{}\n{}", outcome.stdout, outcome.stderr);
        let normalized = combined.to_ascii_lowercase();
        let ready = match self {
            Self::CodexExecJson => {
                outcome.exit_code == 0
                    && normalized.contains("logged in")
                    && !normalized.contains("not logged in")
            }
            Self::ClaudePrintStreamJson => {
                outcome.exit_code == 0
                    && serde_json::from_str::<Value>(&outcome.stdout)
                        .ok()
                        .and_then(|value| value.get("loggedIn").and_then(Value::as_bool))
                        == Some(true)
            }
            Self::AgentsSdkJsonl | Self::PrimeRpc | Self::OmpRpc => {
                if outcome.exit_code == 0
                    && (!outcome.stdout.trim().is_empty() || !outcome.stderr.trim().is_empty())
                {
                    return Ok("unknown");
                }
                false
            }
        };
        ready.then_some("ready").ok_or_else(|| {
            RunnerError::catalog(ErrorCode::HarnessNeedsAuth)
                .with_detail("adapterId", self.id())
                .with_detail("readinessProbeExitCode", outcome.exit_code)
        })
    }

    #[must_use]
    pub fn version_is_supported(self, observed: &str) -> bool {
        let observed = observed.trim();
        let candidate = match self {
            Self::AgentsSdkJsonl => observed.strip_prefix("prose-agents-sdk "),
            Self::CodexExecJson => observed.strip_prefix("codex-cli "),
            Self::ClaudePrintStreamJson => {
                Some(observed.strip_suffix(" (Claude Code)").unwrap_or(observed))
            }
            Self::PrimeRpc => Some(observed.strip_prefix("prime-agent ").unwrap_or(observed)),
            Self::OmpRpc => observed.strip_prefix("omp/"),
        };
        candidate.is_some_and(|version| {
            self.admitted_versions()
                .iter()
                .any(|admitted| admitted == version)
        })
    }

    /// Exact audited functional-alpha versions. This recipe field, rather
    /// than the human-readable range summary, is the admission authority.
    ///
    /// # Panics
    ///
    /// Panics if the embedded, build-time-validated recipe is malformed or
    /// omits its required `support.admittedVersions` field.
    #[must_use]
    pub fn admitted_versions(self) -> Vec<String> {
        let recipe: Value = serde_json::from_str(self.recipe_json())
            .expect("embedded adapter recipe must be valid JSON");
        recipe
            .pointer("/support/admittedVersions")
            .and_then(Value::as_array)
            .expect("embedded adapter recipe must declare support.admittedVersions")
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .expect("admitted adapter versions must be strings")
                    .to_owned()
            })
            .collect()
    }

    /// Return the exact, copyable repair command frozen in the adapter recipe.
    ///
    /// # Panics
    ///
    /// Panics if the embedded, build-time-validated recipe is malformed or
    /// omits its required `support.repairCommand` field.
    #[must_use]
    pub fn repair_command(self) -> String {
        let recipe: Value = serde_json::from_str(self.recipe_json())
            .expect("embedded adapter recipe must be valid JSON");
        recipe
            .pointer("/support/repairCommand")
            .and_then(Value::as_str)
            .expect("embedded adapter recipe must declare support.repairCommand")
            .to_owned()
    }

    #[must_use]
    pub fn incompatible_version_error(self, observed: &str) -> RunnerError {
        RunnerError::catalog(ErrorCode::HarnessIncompatible)
            .with_detail("adapterId", self.id())
            .with_detail("detectedVersion", observed)
            .with_detail("admittedVersions", self.admitted_versions())
            .with_detail("repairCommand", self.repair_command())
            .with_detail("fallbackAttempted", false)
    }

    /// Returns the actionable missing-executable error for this exact
    /// admitted adapter. The repair data comes from the same frozen recipe as
    /// version admission and is safe to expose in machine and human output.
    #[must_use]
    pub fn unavailable_error(self) -> RunnerError {
        RunnerError::catalog(ErrorCode::HarnessUnavailable)
            .with_detail("adapterId", self.id())
            .with_detail("executableNames", self.executable_names())
            .with_detail("admittedVersions", self.admitted_versions())
            .with_detail("repairCommand", self.repair_command())
            .with_detail("fallbackAttempted", false)
    }

    /// Returns the exact runtime prerequisite frozen into this adapter recipe.
    /// OMP has exactly one Bun requirement; every other current adapter has
    /// none.
    ///
    /// # Panics
    ///
    /// Panics if the embedded recipe's prerequisite shape or exact admitted
    /// values drift from the shared product contract.
    #[must_use]
    pub fn runtime_prerequisites(self) -> Vec<RuntimePrerequisiteRequirement> {
        let recipe: Value = serde_json::from_str(self.recipe_json())
            .expect("embedded adapter recipe must be valid JSON");
        let values = recipe.pointer("/support/runtimePrerequisites");
        if self != Self::OmpRpc {
            assert!(
                values.is_none(),
                "only OMP may declare a runtime prerequisite"
            );
            return Vec::new();
        }
        let values = values
            .and_then(Value::as_array)
            .expect("OMP recipe must declare runtimePrerequisites");
        assert_eq!(values.len(), 1, "OMP must declare one runtime prerequisite");
        let value = &values[0];
        assert_eq!(value["runtime"], "bun");
        assert_eq!(value["versionRange"], ">=1.3.14");
        assert_eq!(
            value["repairCommand"],
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9"
        );
        vec![RuntimePrerequisiteRequirement {
            runtime: "bun",
            version_range: ">=1.3.14",
            repair_command: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
        }]
    }

    #[must_use]
    pub const fn recipe_json(self) -> &'static str {
        match self {
            Self::AgentsSdkJsonl => include_str!("../../../../shared/capabilities/adapters/recipes/agents-sdk-jsonl.v1.json"),
            Self::CodexExecJson => if cfg!(any(test, feature = "test-seams")) { include_str!("../../../../shared/capabilities/adapters/recipes/codex-exec-json.v1.json") } else { include_str!("../../../../shared/capabilities/adapters/recipes/codex-exec-json-developer.v1.json") },
            Self::ClaudePrintStreamJson => include_str!(
                "../../../../shared/capabilities/adapters/recipes/claude-print-stream-json.v1.json"
            ),
            Self::PrimeRpc => {
                include_str!("../../../../shared/capabilities/adapters/recipes/prime-rpc.v1.json")
            }
            Self::OmpRpc => {
                include_str!("../../../../shared/capabilities/adapters/recipes/omp-rpc.v1.json")
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HostPlatform<'a> {
    pub os: &'a str,
    pub arch: &'a str,
    pub libc: Option<&'a str>,
}

impl HostPlatform<'static> {
    #[must_use]
    pub const fn current() -> Self {
        Self {
            os: std::env::consts::OS,
            arch: std::env::consts::ARCH,
            libc: if cfg!(target_env = "musl") {
                Some("musl")
            } else if cfg!(target_os = "linux") {
                Some("gnu")
            } else {
                None
            },
        }
    }
}

/// Refuses an installed adapter on any host target not named by its frozen
/// recipe. The explicit host value makes cross-platform admission testable
/// without mutating process or user configuration.
///
/// # Errors
///
/// Returns `HARNESS_INCOMPATIBLE` when the host has no declared recipe target.
///
/// # Panics
///
/// Panics only when a compile-time embedded recipe is invalid or omits its
/// required platform list; shared schema tests prevent either release state.
pub fn assert_platform_supported(
    adapter: InstalledAdapter,
    host: HostPlatform<'_>,
) -> Result<String, RunnerError> {
    let arch = match host.arch {
        "aarch64" | "arm64" => Some("arm64"),
        "x86_64" | "x64" => Some("x64"),
        _ => None,
    };
    let candidates = arch.map_or_else(Vec::new, |arch| match host.os {
        "macos" | "darwin" => vec![format!("darwin-{arch}")],
        "windows" | "win32" => vec![format!("win32-{arch}")],
        "linux" => {
            let mut values = Vec::new();
            if let Some(libc) = host.libc {
                values.push(format!("linux-{arch}-{libc}"));
            }
            if host.libc == Some("gnu") {
                values.push(format!("linux-{arch}-musl"));
            }
            values.push(format!("linux-{arch}"));
            values
        }
        _ => Vec::new(),
    });
    let recipe: Value = serde_json::from_str(adapter.recipe_json())
        .expect("embedded adapter recipe must be valid JSON");
    let supported = recipe
        .pointer("/support/platforms")
        .and_then(Value::as_array)
        .expect("embedded adapter recipe must declare support.platforms");
    if let Some(platform) = candidates.iter().find(|candidate| {
        supported
            .iter()
            .any(|value| value.as_str() == Some(candidate.as_str()))
    }) {
        return Ok(platform.clone());
    }
    Err(RunnerError::catalog(ErrorCode::HarnessIncompatible)
        .with_detail("adapterId", adapter.id())
        .with_detail(
            "hostPlatform",
            match host.os {
                "macos" => "darwin",
                "windows" => "win32",
                other => other,
            },
        )
        .with_detail("hostArchitecture", arch.unwrap_or(host.arch))
        .with_detail("supportedPlatforms", supported.clone())
        .with_detail("fallbackAttempted", false))
}

/// An exact, shell-free launch plan. The private-file guard is intentionally
/// retained until after the process has exited.
#[derive(Debug)]
pub struct PreparedLaunch {
    pub adapter: InstalledAdapter,
    pub executable: PathBuf,
    pub argv: Vec<OsString>,
    pub stdin: Option<Vec<u8>>,
    pub environment: EnvironmentPolicy,
    prompt_files: Option<PrivatePromptFiles>,
    daemon_files: Option<PrivatePromptFiles>,
    daemon_socket_path: Option<PathBuf>,
    credential_config_directory: Option<PathBuf>,
    omp_control_overlay_path: Option<PathBuf>,
}

impl PreparedLaunch {
    /// Opt-in native tool selection; the default launch is left byte-for-byte unchanged.
    pub fn apply_workspace_profile(&mut self, auth_group:&str, dirs:&[String], rules:&[String])->Result<(),RunnerError>{
        if self.adapter != InstalledAdapter::ClaudePrintStreamJson {return Err(RunnerError::config("Workspace profile requires Claude."));}
        if rules.iter().any(|v|v.trim().is_empty() || v.contains('\0') || v.starts_with('-')) {return Err(RunnerError::config("Invalid native tool permission rule."));}
        self.argv.retain(|arg|arg != "--bare");
        let mut flags:Vec<OsString>=vec!["--setting-sources".into(),"".into(),"--tools".into(),"Read,Write,Edit,Glob,Grep,Agent,Bash".into()];
        for directory in dirs {flags.extend(["--add-dir".into(),directory.into()]);}
        for rule in rules {flags.extend(["--allowedTools".into(),rule.into()]);}
        self.argv.splice(0..0,flags);
        if auth_group == "anthropic-api-key" {
            let files=self.prompt_files.as_ref().ok_or_else(||RunnerError::catalog(ErrorCode::InternalRunnerFault))?;
            let directory=files.create_credential_config_directory().map_err(|_|RunnerError::catalog(ErrorCode::InternalRunnerFault).with_detail("reason","Cannot create private native config"))?;
            // Closed environment excludes inherited SIMPLE, config overrides and competing credentials.
            self.environment=self.environment.clone().set("CLAUDE_CONFIG_DIR",directory.as_os_str(),Sensitivity::Secret);
            self.credential_config_directory=Some(directory);
        }
        assert_argv_limits(self.adapter,&self.executable,&self.argv,HostPlatform::current())
    }

    #[must_use]
    pub fn image_path(&self) -> Option<&Path> {
        self.prompt_files
            .as_ref()
            .map(PrivatePromptFiles::image_path)
    }

    #[must_use]
    pub fn daemon_socket_path(&self) -> Option<&Path> {
        self.daemon_socket_path.as_deref()
    }

    #[must_use]
    pub fn daemon_directory(&self) -> Option<&Path> {
        self.daemon_files
            .as_ref()
            .map(PrivatePromptFiles::directory)
    }

    #[must_use]
    pub fn credential_config_directory(&self) -> Option<&Path> {
        self.credential_config_directory.as_deref()
    }

    #[must_use]
    pub fn omp_control_overlay_path(&self) -> Option<&Path> {
        self.omp_control_overlay_path.as_deref()
    }

    #[must_use]
    pub fn omp_prompt_bytes(&self) -> Option<&[u8]> {
        (self.adapter == InstalledAdapter::OmpRpc)
            .then_some(self.stdin.as_deref())
            .flatten()
    }

    pub fn preserve_daemon_files(&mut self) {
        if let Some(files) = self.daemon_files.as_mut() {
            files.preserve_directory();
        }
    }

    /// Removes every private prompt, task, overlay, and credential file owned
    /// by this launch and verifies that each private root is absent. A runner
    /// must settle this result before publishing any terminal result.
    ///
    /// # Errors
    ///
    /// Returns a fixed, pathless cleanup error when any private root cannot be
    /// removed or remains observable after removal.
    pub fn finalize_private_files(&mut self) -> Result<(), RunnerError> {
        let adapter = self.adapter;
        for files in [&mut self.prompt_files, &mut self.daemon_files] {
            if let Some(files) = files.as_mut() {
                files
                    .close()
                    .map_err(|_| private_file_cleanup_failure(adapter))?;
            }
        }
        Ok(())
    }

    #[must_use]
    pub fn private_file_cleanup_failure(&self) -> RunnerError {
        private_file_cleanup_failure(self.adapter)
    }

    #[must_use]
    pub fn process_spec(
        &self,
        cwd: PathBuf,
        wrapper_executable: Option<PathBuf>,
        invocation_id: &str,
        run_timeout: Duration,
        probe_version: bool,
        cancellation: CancellationToken,
    ) -> ProcessSpec {
        ProcessSpec {
            executable: self.executable.clone(),
            argv: self.argv.clone(),
            cwd,
            wrapper_executable,
            version_probe: probe_version.then(|| self.adapter.version_probe()),
            stdin: if self.adapter == InstalledAdapter::OmpRpc {
                Some(Vec::new())
            } else {
                self.stdin.clone()
            },
            stdin_lifecycle: self.adapter.recipe_stdin_lifecycle(),
            environment: self.environment.clone(),
            invocation_id: invocation_id.to_owned(),
            recursion_token: format!("installed-recursion-{invocation_id}"),
            run_nonce: format!("installed-run-{invocation_id}"),
            startup_timeout: run_timeout.min(Duration::from_secs(30)),
            run_timeout,
            termination_grace: Duration::from_secs(2),
            limits: StreamLimits::default(),
            cancellation,
            cancel_after_start: None,
        }
    }
}

fn private_file_cleanup_failure(adapter: InstalledAdapter) -> RunnerError {
    RunnerError::catalog(ErrorCode::ProcessCleanupFailed)
        .with_detail("phase", "private-file-finalization")
        .with_detail("resource", "owned-private-transport-files")
        .with_detail("adapterId", adapter.id())
        .with_detail("fallbackAttempted", false)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransportNormalization {
    pub terminal_event: &'static str,
    pub assistant_messages: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveredTerminal {
    pub envelope: Value,
    pub visible_messages: Vec<String>,
    pub visible_text: String,
}

/// Projects only complete assistant text values from one already-framed
/// harness record.
///
/// This deliberately does not make a lifecycle or terminal claim. It is used
/// by human-mode streaming with a final-nonblank-line holdback; the ordinary
/// full normalizer remains the authority for settlement.
pub(crate) fn admitted_assistant_messages(
    adapter: InstalledAdapter,
    records: &[Value],
    expected_rpc_id: &str,
) -> Result<Vec<String>, RunnerError> {
    let malformed = || RunnerError::catalog(ErrorCode::ProtocolMalformed);
    let Some(record) = records.last() else {
        return Ok(Vec::new());
    };
    if matches!(adapter, InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc) && records.iter().any(native_tools::rich) {
        return Ok(native_tools::normalize(records,expected_rpc_id,adapter==InstalledAdapter::OmpRpc,false)?.assistant_messages);
    }

    match adapter {
        InstalledAdapter::AgentsSdkJsonl => {
            if record_type(record) != Some("final") { return Ok(Vec::new()); }
            Ok(normalize_transport(adapter, records, expected_rpc_id)?.assistant_messages)
        }
        InstalledAdapter::CodexExecJson => {
            if record_type(record) != Some("item.completed") {
                return Ok(Vec::new());
            }
            let item = record
                .get("item")
                .and_then(Value::as_object)
                .ok_or_else(malformed)?;
            if item.get("type").and_then(Value::as_str) != Some("agent_message") {
                return Ok(Vec::new());
            }
            item.get("text")
                .and_then(Value::as_str)
                .ok_or_else(malformed)?;
            let mut completed = records.to_vec();
            completed.push(json!({"type":"turn.completed"}));
            Ok(normalize_transport(adapter, &completed, expected_rpc_id)?.assistant_messages)
        }
        InstalledAdapter::ClaudePrintStreamJson => {
            if record_type(record) == Some("assistant") {
                let session_id = records
                    .first()
                    .and_then(|record| record.get("session_id"))
                    .and_then(Value::as_str)
                    .ok_or_else(malformed)?;
                let mut completed = records.to_vec();
                completed.push(json!({
                    "type":"result",
                    "subtype":"success",
                    "session_id":session_id,
                    "is_error":false
                }));
                Ok(normalize_transport(adapter, &completed, expected_rpc_id)?.assistant_messages)
            } else {
                Ok(Vec::new())
            }
        }
        InstalledAdapter::PrimeRpc => {
            let update_type = record
                .get("assistantMessageEvent")
                .and_then(|event| event.get("type"))
                .and_then(Value::as_str);
            let is_text_snapshot = record_type(record) == Some("message_update")
                && matches!(update_type, Some("text_delta" | "text_end"));
            let is_message_end = record_type(record) == Some("message_end")
                && record.pointer("/message/role").and_then(Value::as_str) == Some("assistant");
            if !is_text_snapshot && !is_message_end {
                return Ok(Vec::new());
            }
            let message = record.get("message").ok_or_else(malformed)?.clone();
            let text_index = prime_text_index(&message).ok_or_else(malformed)?;
            let user = records
                .iter()
                .find(|record| {
                    record_type(record) == Some("message_end")
                        && record.pointer("/message/role").and_then(Value::as_str) == Some("user")
                })
                .and_then(|record| record.get("message"))
                .cloned()
                .ok_or_else(malformed)?;
            let mut completed = records.to_vec();
            if update_type == Some("text_delta") {
                let text = prime_assistant_text(&message, text_index).ok_or_else(malformed)?;
                completed.push(json!({
                    "type":"message_update",
                    "assistantMessageEvent":{
                        "type":"text_end",
                        "contentIndex":text_index,
                        "content":text
                    },
                    "message":message.clone()
                }));
            }
            if !is_message_end {
                completed.push(json!({"type":"message_end","message":message.clone()}));
            }
            completed.push(json!({"type":"turn_end","message":message.clone(),"toolResults":[]}));
            completed.push(json!({"type":"agent_end","messages":[user,message]}));
            Ok(normalize_transport(adapter, &completed, expected_rpc_id)?.assistant_messages)
        }
        InstalledAdapter::OmpRpc => {
            let is_text_delta = record_type(record) == Some("message_update")
                && record
                    .pointer("/assistantMessageEvent/type")
                    .and_then(Value::as_str)
                    == Some("text_delta");
            let is_message_end = record_type(record) == Some("message_end")
                && record.pointer("/message/role").and_then(Value::as_str) == Some("assistant");
            if !is_text_delta && !is_message_end {
                return Ok(Vec::new());
            }
            let message = if is_message_end {
                record.get("message").ok_or_else(malformed)?.clone()
            } else {
                let text = records
                    .iter()
                    .filter(|record| {
                        record_type(record) == Some("message_update")
                            && record
                                .pointer("/assistantMessageEvent/type")
                                .and_then(Value::as_str)
                                == Some("text_delta")
                    })
                    .map(|record| {
                        record
                            .pointer("/assistantMessageEvent/delta")
                            .and_then(Value::as_str)
                            .ok_or_else(malformed)
                    })
                    .collect::<Result<String, RunnerError>>()?;
                json!({"role":"assistant","content":[{"type":"text","text":text}]})
            };
            let user = records
                .iter()
                .find(|record| {
                    record_type(record) == Some("message_end")
                        && record.pointer("/message/role").and_then(Value::as_str) == Some("user")
                })
                .and_then(|record| record.get("message"))
                .cloned()
                .ok_or_else(malformed)?;
            let response_seen = records.iter().any(|record| {
                record_type(record) == Some("response")
                    && record.get("command").and_then(Value::as_str) == Some("prompt")
            });
            let mut completed = records.to_vec();
            if !is_message_end {
                completed.push(json!({"type":"message_end","message":message.clone()}));
            }
            completed.push(json!({"type":"turn_end","message":message.clone(),"toolResults":[]}));
            completed.push(json!({"type":"agent_end","messages":[user,message],"isTerminal":true}));
            if !response_seen {
                completed.push(json!({
                    "id":omp_rpc_id(expected_rpc_id, "prompt.1"),
                    "type":"response",
                    "command":"prompt",
                    "success":true
                }));
            }
            Ok(normalize_transport(adapter, &completed, expected_rpc_id)?.assistant_messages)
        }
    }
}

/// Structurally validates an adapter stream without interpreting assistant
/// text as `OpenProse` language output. A valid harness terminal is necessary
/// transport evidence, never a semantic-success claim.
///
/// # Errors
///
/// Returns `PROTOCOL_MALFORMED` when ordering, correlation, or the required
/// transport terminal differs from the frozen adapter protocol.
pub fn normalize_transport(
    adapter: InstalledAdapter,
    records: &[Value],
    expected_rpc_id: &str,
) -> Result<TransportNormalization, RunnerError> {
    normalize_transport_mode(adapter, records, expected_rpc_id, false)
}

pub(crate) fn validate_prime_native_prefix(records:&[Value],id:&str)->Result<(),RunnerError>{native_tools::normalize_mode(records,id,false,false,true).map(|_|())}

pub fn normalize_transport_mode(adapter:InstalledAdapter,records:&[Value],expected_rpc_id:&str,native_claude:bool)->Result<TransportNormalization,RunnerError>{
    let malformed = || RunnerError::catalog(ErrorCode::ProtocolMalformed);
    let failed = || RunnerError::catalog(ErrorCode::HarnessFailed);
    match adapter {
        InstalledAdapter::CodexExecJson => {
            let Some(thread_id) = records
                .first()
                .filter(|record| record_type(record) == Some("thread.started"))
                .and_then(|record| record.get("thread_id"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
            else {
                return Err(malformed());
            };
            let mut turn_started = false;
            let mut assistant_messages = Vec::new();
            for (index, record) in records.iter().enumerate().skip(1) {
                if record
                    .get("thread_id")
                    .and_then(Value::as_str)
                    .is_some_and(|observed| observed != thread_id)
                {
                    return Err(malformed());
                }
                match record_type(record) {
                    Some("turn.started") if !turn_started => turn_started = true,
                    Some("turn.failed" | "error") => return Err(failed()),
                    Some("item.started" | "item.updated") if turn_started => {}
                    Some("item.completed") if turn_started => {
                        let item = record
                            .get("item")
                            .and_then(Value::as_object)
                            .ok_or_else(malformed)?;
                        let item_type = item
                            .get("type")
                            .and_then(Value::as_str)
                            .ok_or_else(malformed)?;
                        if item_type == "agent_message" {
                            assistant_messages.push(
                                item.get("text")
                                    .and_then(Value::as_str)
                                    .ok_or_else(malformed)?
                                    .to_owned(),
                            );
                        }
                    }
                    Some("turn.completed") if turn_started && index + 1 == records.len() => {}
                    _ => return Err(malformed()),
                }
            }
            if !turn_started || records.last().and_then(record_type) != Some("turn.completed") {
                return Err(malformed());
            }
            Ok(TransportNormalization {
                terminal_event: "turn.completed",
                assistant_messages,
            })
        }
        InstalledAdapter::AgentsSdkJsonl => {
            if records.first().and_then(record_type) != Some("start") || records.first().and_then(|r|r.get("model")).and_then(Value::as_str).is_none() || records.first().and_then(|r|r.get("cwd")).and_then(Value::as_str).is_none() {return Err(malformed());}
            for r in records.iter().skip(1).take(records.len().saturating_sub(2)) {
                if !matches!(record_type(r),Some("tool_call"|"tool_result")) || r.get("name").and_then(Value::as_str).is_none() {return Err(malformed());}
            }
            let last=records.last().ok_or_else(malformed)?;
            if record_type(last)==Some("error") {return Err(failed().with_detail("nativeFailure",sdk_native_failure(last)));}
            if records.len()<2 || record_type(last)!=Some("final") {return Err(malformed());}
            let output=last.get("output").and_then(Value::as_str).ok_or_else(malformed)?;
            Ok(TransportNormalization{terminal_event:"final",assistant_messages:vec![output.to_owned()]})
        }
        InstalledAdapter::ClaudePrintStreamJson => {
            if native_claude && records.iter().filter(|r|r["type"]=="system" && r["subtype"]=="init").any(|r|r.get("messaging_socket_path").is_some_and(|v|!v.as_str().is_some_and(|s|!s.is_empty()))) {return Err(malformed());}
            let Some(session_id) = records
                .first()
                .filter(|record| {
                    record_type(record) == Some("system")
                        && record.get("subtype").and_then(Value::as_str) == Some("init")
                })
                .and_then(|record| record.get("session_id"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
            else {
                return Err(malformed());
            };
            let mut assistant_messages = Vec::new();
            for (index, record) in records.iter().enumerate().skip(1) {
                if record.get("session_id").and_then(Value::as_str) != Some(session_id) {
                    return Err(malformed());
                }
                match record_type(record) {
                    Some("assistant") => {
                        assistant_messages.extend(assistant_text_content(record)?);
                    }
                    Some("user" | "stream_event" | "tool_progress") => {}
                    Some("system") if record.get("subtype").and_then(Value::as_str)==Some("init") => {
                        let previous=&records[index-1];
                        if (!native_claude && previous.get("subtype").and_then(Value::as_str)!=Some("task_notification")) || !record.get("uuid").and_then(Value::as_str).is_some_and(|v|!v.is_empty()) {return Err(malformed());}
                        let mut first=records[0].as_object().ok_or_else(malformed)?.clone();let mut repeated=record.as_object().ok_or_else(malformed)?.clone();first.remove("uuid");repeated.remove("uuid");if native_claude{first.remove("messaging_socket_path");repeated.remove("messaging_socket_path");}if first!=repeated{return Err(malformed());}
                    }
                    Some("system") if record.get("subtype").and_then(Value::as_str)==Some("background_tasks_changed") => {
                        if !record.get("uuid").and_then(Value::as_str).is_some_and(|v|!v.is_empty()) {return Err(malformed());}
                        let tasks=record.get("tasks").and_then(Value::as_array).ok_or_else(malformed)?;
                        if !tasks.iter().all(|task|["task_id","description","task_type"].iter().all(|key|task.get(*key).and_then(Value::as_str).is_some_and(|v|!v.is_empty()))) {return Err(malformed());}
                    }
                    Some("system") if matches!(record.get("subtype").and_then(Value::as_str),Some("task_started"|"task_progress"|"task_updated"|"task_notification")) => {
                        if !["task_id","uuid"].iter().all(|key|record.get(*key).and_then(Value::as_str).is_some_and(|v|!v.is_empty())) {return Err(malformed());}
                        match record.get("subtype").and_then(Value::as_str) {
                            Some("task_started") if record.get("description").and_then(Value::as_str).is_none() => return Err(malformed()),
                            Some("task_updated") if !record.get("patch").is_some_and(Value::is_object) => return Err(malformed()),
                            Some("task_notification") if !matches!(record.get("status").and_then(Value::as_str),Some("completed"|"failed"|"stopped")) => return Err(malformed()),
                            Some("task_progress") => {
                                let usage=record.get("usage").ok_or_else(malformed)?;
                                if !["total_tokens","tool_uses","duration_ms"].iter().all(|key|usage.get(*key).and_then(Value::as_u64).is_some_and(|v|v<=9_007_199_254_740_991)) {return Err(malformed());}
                            }
                            _ => {}
                        }
                        // Child task completion does not settle the outer invocation.
                    }
                    Some("system") if record.get("subtype").and_then(Value::as_str) == Some("permission_denied") => {
                        if !["tool_name","tool_use_id","message"].iter().all(|key|record.get(*key).and_then(Value::as_str).is_some()){return Err(malformed());}
                    }
                    Some("system")
                        if record.get("subtype").and_then(Value::as_str)
                            == Some("thinking_tokens") =>
                    {
                        let valid = record.as_object().is_some_and(|object| {
                            object.len() == 6
                                && [
                                    "type",
                                    "subtype",
                                    "estimated_tokens",
                                    "estimated_tokens_delta",
                                    "uuid",
                                    "session_id",
                                ]
                                .iter()
                                .all(|key| object.contains_key(*key))
                        }) && record
                            .get("uuid")
                            .and_then(Value::as_str)
                            .is_some_and(|value| !value.is_empty())
                            && ["estimated_tokens", "estimated_tokens_delta"]
                                .iter()
                                .all(|key| {
                                    record
                                        .get(*key)
                                        .and_then(Value::as_u64)
                                        .is_some_and(|value| value <= 9_007_199_254_740_991)
                                });
                        if !valid {
                            return Err(malformed());
                        }
                    }
                    Some("result") if native_claude || index + 1 == records.len() => {
                        if record.get("subtype").and_then(Value::as_str) != Some("success")
                            || record.get("is_error").and_then(Value::as_bool) != Some(false)
                        {
                            return Err(failed());
                        }
                    }
                    _ => return Err(malformed()),
                }
            }
            if (native_claude && !claude_shutdown::has_fresh_result(records)) || (!native_claude && records.last().and_then(record_type) != Some("result")) {
                return Err(malformed());
            }
            Ok(TransportNormalization {
                terminal_event: "result(subtype=success,is_error=false)",
                assistant_messages,
            })
        }
        InstalledAdapter::PrimeRpc if native_claude => native_tools::normalize_mode(records,expected_rpc_id,false,true,true),
        InstalledAdapter::PrimeRpc => normalize_prime_rpc(records, expected_rpc_id),
        InstalledAdapter::OmpRpc => normalize_omp_rpc(records, expected_rpc_id),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PrimeLifecycle {
    AwaitPromptAck,
    AwaitAgentStart,
    AwaitTurnStart,
    AwaitUserMessageStart,
    AwaitUserMessageEnd,
    AwaitAssistantMessageStart,
    AwaitThinkingOrTextStart,
    AwaitThinkingEnd,
    ThinkingDelta,
    AwaitTextStart,
    AwaitTextDelta,
    TextDelta,
    AwaitAssistantMessageEnd,
    AwaitTurnEnd,
    AwaitAgentEnd,
    Complete,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct PrimeDiagnosticCounters {
    accepted_records: u32,
    thinking_deltas: u32,
    text_deltas: u32,
    saturated: bool,
}

impl PrimeDiagnosticCounters {
    fn accept(&mut self, record: &Value) {
        self.accepted_records =
            saturating_diagnostic_count(self.accepted_records, &mut self.saturated);
        let update_type = record
            .get("assistantMessageEvent")
            .and_then(|event| event.get("type"))
            .and_then(Value::as_str);
        match update_type {
            Some("thinking_delta") => {
                self.thinking_deltas =
                    saturating_diagnostic_count(self.thinking_deltas, &mut self.saturated);
            }
            Some("text_delta") => {
                self.text_deltas =
                    saturating_diagnostic_count(self.text_deltas, &mut self.saturated);
            }
            _ => {}
        }
    }
}

const fn saturating_diagnostic_count(value: u32, saturated: &mut bool) -> u32 {
    if value == u32::MAX {
        *saturated = true;
        value
    } else {
        value + 1
    }
}

fn prime_phase(lifecycle: PrimeLifecycle) -> &'static str {
    match lifecycle {
        PrimeLifecycle::AwaitPromptAck => "await-prompt-ack",
        PrimeLifecycle::AwaitAgentStart => "await-agent-start",
        PrimeLifecycle::AwaitTurnStart => "await-turn-start",
        PrimeLifecycle::AwaitUserMessageStart => "await-user-message-start",
        PrimeLifecycle::AwaitUserMessageEnd => "await-user-message-end",
        PrimeLifecycle::AwaitAssistantMessageStart => "await-assistant-message-start",
        PrimeLifecycle::AwaitThinkingOrTextStart => "await-thinking-or-text-start",
        PrimeLifecycle::AwaitThinkingEnd | PrimeLifecycle::ThinkingDelta => {
            "await-thinking-delta-or-end"
        }
        PrimeLifecycle::AwaitTextStart => "await-text-start",
        PrimeLifecycle::AwaitTextDelta | PrimeLifecycle::TextDelta => "await-text-delta-or-end",
        PrimeLifecycle::AwaitAssistantMessageEnd => "await-assistant-message-end",
        PrimeLifecycle::AwaitTurnEnd => "await-turn-end",
        PrimeLifecycle::AwaitAgentEnd => "await-agent-end",
        PrimeLifecycle::Complete => "complete",
    }
}

fn prime_adapter_diagnostic(
    lifecycle: PrimeLifecycle,
    counters: PrimeDiagnosticCounters,
    framing: bool,
) -> Value {
    json!({
        "schema":"openprose.adapter-diagnostic/1",
        "adapterId":"prime/rpc",
        "stage":if framing { "jsonl-framing" } else { "prime-lifecycle" },
        "phase":if framing { "record-boundary" } else { prime_phase(lifecycle) },
        "counters":{
            "acceptedRecords":counters.accepted_records,
            "thinkingDeltas":counters.thinking_deltas,
            "textDeltas":counters.text_deltas,
            "saturated":counters.saturated,
        }
    })
}

fn prime_protocol_error(
    lifecycle: PrimeLifecycle,
    counters: PrimeDiagnosticCounters,
) -> RunnerError {
    RunnerError::catalog(ErrorCode::ProtocolMalformed).with_detail(
        "adapterDiagnostic",
        prime_adapter_diagnostic(lifecycle, counters, false),
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PrimeContentShape {
    Empty,
    EmptyText,
    Text,
    ThinkingEmpty,
    Thinking,
    ThinkingAndEmptyText,
    ThinkingAndText,
}

fn normalize_prime_rpc(
    records: &[Value],
    expected_rpc_id: &str,
) -> Result<TransportNormalization, RunnerError> {
    if records.iter().any(native_tools::rich) { return native_tools::normalize(records, expected_rpc_id, false, true); }
    let mut lifecycle = PrimeLifecycle::AwaitPromptAck;
    let mut counters = PrimeDiagnosticCounters::default();
    macro_rules! malformed {
        () => {
            prime_protocol_error(lifecycle, counters)
        };
    }
    macro_rules! malformed_because {
        ($reason:expr $(,)?) => {{
            let _ = $reason;
            malformed!()
        }};
    }
    let failed = || RunnerError::catalog(ErrorCode::HarnessFailed);
    if records.iter().any(|record| !prime_bounded_json(record, 0)) {
        return Err(malformed_because!(
            "Prime RPC record exceeds the bounded lifecycle contract.",
        ));
    }
    let Some(response) = records.first().filter(|record| {
        has_exact_keys(record, &["id", "type", "command", "success"])
            && record_type(record) == Some("response")
            && record.get("id").and_then(Value::as_str) == Some(expected_rpc_id)
            && record.get("command").and_then(Value::as_str) == Some("prompt")
    }) else {
        return Err(malformed_because!(
            "Prime prompt acknowledgement is invalid.",
        ));
    };
    if response.get("success").and_then(Value::as_bool) != Some(true) {
        return Err(failed());
    }

    counters.accept(response);
    lifecycle = PrimeLifecycle::AwaitAgentStart;
    let mut user_message: Option<Value> = None;
    let mut assistant_message: Option<Value> = None;
    let mut assistant_thinking = String::new();
    let mut assistant_text = String::new();
    let mut text_index: Option<usize> = None;
    for record in records.iter().skip(1) {
        if record_type(record) == Some("extension_ui_request") {
            return Err(failed());
        }
        match record_type(record) {
            Some("agent_start")
                if lifecycle == PrimeLifecycle::AwaitAgentStart
                    && has_exact_keys(record, &["type"]) =>
            {
                lifecycle = PrimeLifecycle::AwaitTurnStart;
            }
            Some("turn_start")
                if lifecycle == PrimeLifecycle::AwaitTurnStart
                    && has_exact_keys(record, &["type"]) =>
            {
                lifecycle = PrimeLifecycle::AwaitUserMessageStart;
            }
            Some("message_start")
                if lifecycle == PrimeLifecycle::AwaitUserMessageStart
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                let message = record.get("message").ok_or_else(|| malformed!())?;
                if !valid_prime_user_message(message) {
                    return Err(malformed_because!(
                        "Prime message_start role or ordering is invalid.",
                    ));
                }
                user_message = Some(message.clone());
                lifecycle = PrimeLifecycle::AwaitUserMessageEnd;
            }
            Some("message_end")
                if lifecycle == PrimeLifecycle::AwaitUserMessageEnd
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                if record.get("message") != user_message.as_ref() {
                    return Err(malformed_because!(
                        "Prime message_end role or ordering is invalid.",
                    ));
                }
                lifecycle = PrimeLifecycle::AwaitAssistantMessageStart;
            }
            Some("message_start")
                if lifecycle == PrimeLifecycle::AwaitAssistantMessageStart
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                let message = record.get("message").ok_or_else(|| malformed!())?;
                if !valid_prime_assistant_message(message)
                    || !prime_content_has_shape(message, PrimeContentShape::Empty)
                {
                    return Err(malformed_because!(
                        "Prime message_start role or ordering is invalid.",
                    ));
                }
                lifecycle = PrimeLifecycle::AwaitThinkingOrTextStart;
            }
            Some("message_update") => {
                if !has_exact_keys(record, &["type", "assistantMessageEvent", "message"]) {
                    return Err(malformed_because!(
                        "Prime message_update carried unknown or missing fields.",
                    ));
                }
                let message = record.get("message").ok_or_else(|| malformed!())?;
                let update = record
                    .get("assistantMessageEvent")
                    .ok_or_else(|| malformed!())?;
                if !valid_prime_assistant_message(message) {
                    return Err(malformed_because!(
                        "Prime message_update current assistant message is invalid.",
                    ));
                }
                match (lifecycle, record_type(update)) {
                    (PrimeLifecycle::AwaitThinkingOrTextStart, Some("thinking_start"))
                        if has_exact_keys(update, &["type", "contentIndex"])
                            && update.get("contentIndex").and_then(Value::as_u64) == Some(0)
                            && prime_content_has_shape(
                                message,
                                PrimeContentShape::ThinkingEmpty,
                            ) =>
                    {
                        lifecycle = PrimeLifecycle::AwaitThinkingEnd;
                    }
                    (PrimeLifecycle::AwaitThinkingOrTextStart, Some("text_start"))
                        if has_exact_keys(update, &["type", "contentIndex"])
                            && update.get("contentIndex").and_then(Value::as_u64) == Some(0)
                            && prime_content_has_shape(message, PrimeContentShape::EmptyText) =>
                    {
                        text_index = Some(0);
                        lifecycle = PrimeLifecycle::AwaitTextDelta;
                    }
                    (
                        PrimeLifecycle::AwaitThinkingEnd | PrimeLifecycle::ThinkingDelta,
                        Some("thinking_delta"),
                    ) if has_exact_keys(update, &["type", "contentIndex", "delta"])
                        && update.get("contentIndex").and_then(Value::as_u64) == Some(0)
                        && prime_content_has_shape(message, PrimeContentShape::Thinking) =>
                    {
                        let delta = update
                            .get("delta")
                            .and_then(Value::as_str)
                            .ok_or_else(|| malformed!())?;
                        if assistant_thinking.len().saturating_add(delta.len())
                            > PRIME_RPC_STRING_BYTES_LIMIT
                        {
                            return Err(malformed_because!(
                                "Prime thinking_delta does not match its current assistant message.",
                            ));
                        }
                        assistant_thinking.push_str(delta);
                        if message
                            .pointer("/content/0/thinking")
                            .and_then(Value::as_str)
                            != Some(assistant_thinking.as_str())
                        {
                            return Err(malformed_because!(
                                "Prime thinking_delta does not match its current assistant message.",
                            ));
                        }
                        lifecycle = PrimeLifecycle::ThinkingDelta;
                    }
                    (
                        PrimeLifecycle::AwaitThinkingEnd | PrimeLifecycle::ThinkingDelta,
                        Some("thinking_end"),
                    ) if has_exact_keys(update, &["type", "contentIndex", "content"])
                        && update.get("contentIndex").and_then(Value::as_u64) == Some(0)
                        && prime_content_has_shape(message, PrimeContentShape::Thinking) =>
                    {
                        let content = update
                            .get("content")
                            .and_then(Value::as_str)
                            .ok_or_else(|| malformed!())?;
                        let message_matches = message
                            .pointer("/content/0/thinking")
                            .and_then(Value::as_str)
                            == Some(content);
                        let stream_matches = lifecycle != PrimeLifecycle::ThinkingDelta
                            || assistant_thinking == content
                            || assistant_thinking.strip_suffix("\n\n") == Some(content);
                        if !message_matches || !stream_matches {
                            return Err(malformed_because!(
                                "Prime thinking_end does not settle its cumulative thinking stream.",
                            ));
                        }
                        assistant_thinking.clear();
                        assistant_thinking.push_str(content);
                        lifecycle = PrimeLifecycle::AwaitTextStart;
                    }
                    (PrimeLifecycle::AwaitTextStart, Some("text_start"))
                        if has_exact_keys(update, &["type", "contentIndex"])
                            && update.get("contentIndex").and_then(Value::as_u64) == Some(1)
                            && prime_content_has_shape(
                                message,
                                PrimeContentShape::ThinkingAndEmptyText,
                            ) =>
                    {
                        text_index = Some(1);
                        lifecycle = PrimeLifecycle::AwaitTextDelta;
                    }
                    (
                        PrimeLifecycle::AwaitTextDelta | PrimeLifecycle::TextDelta,
                        Some("text_delta"),
                    ) if has_exact_keys(update, &["type", "contentIndex", "delta"])
                        && text_index.is_some_and(|index| {
                            update.get("contentIndex").and_then(Value::as_u64) == Some(index as u64)
                                && prime_content_has_text_shape(message, index, false)
                        }) =>
                    {
                        let delta = update
                            .get("delta")
                            .and_then(Value::as_str)
                            .ok_or_else(|| malformed!())?;
                        if assistant_text.len().saturating_add(delta.len())
                            > PRIME_RPC_STRING_BYTES_LIMIT
                        {
                            return Err(malformed_because!(
                                "Prime text_delta does not match its current assistant message.",
                            ));
                        }
                        assistant_text.push_str(delta);
                        if prime_assistant_text(message, text_index.unwrap())
                            != Some(assistant_text.as_str())
                        {
                            return Err(malformed_because!(
                                "Prime text_delta does not match its current assistant message.",
                            ));
                        }
                        lifecycle = PrimeLifecycle::TextDelta;
                    }
                    (
                        PrimeLifecycle::AwaitTextDelta | PrimeLifecycle::TextDelta,
                        Some("text_end"),
                    ) if has_exact_keys(update, &["type", "contentIndex", "content"])
                        && text_index.is_some_and(|index| {
                            update.get("contentIndex").and_then(Value::as_u64) == Some(index as u64)
                        })
                        && update.get("content").and_then(Value::as_str).is_some_and(
                            |content| {
                                !content.is_empty()
                                    && (assistant_text.is_empty() || content == assistant_text)
                            },
                        )
                        && text_index.is_some_and(|index| {
                            prime_content_has_text_shape(message, index, false)
                                && prime_assistant_text(message, index)
                                    == update.get("content").and_then(Value::as_str)
                        }) =>
                    {
                        let content = update.get("content").and_then(Value::as_str).unwrap();
                        assistant_text.clear();
                        assistant_text.push_str(content);
                        lifecycle = PrimeLifecycle::AwaitAssistantMessageEnd;
                    }
                    _ => {
                        return Err(malformed_because!(
                            "Prime assistant stream update is missing, duplicate, or out of order.",
                        ));
                    }
                }
            }
            Some("message_end")
                if lifecycle == PrimeLifecycle::AwaitAssistantMessageEnd
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                let message = record.get("message").ok_or_else(|| malformed!())?;
                if !valid_prime_assistant_message(message)
                    || text_index.is_none_or(|index| {
                        prime_assistant_text(message, index) != Some(assistant_text.as_str())
                    })
                {
                    return Err(malformed_because!(
                        "Prime message_end role or ordering is invalid.",
                    ));
                }
                assistant_message = Some(message.clone());
                lifecycle = PrimeLifecycle::AwaitTurnEnd;
            }
            Some("turn_end")
                if lifecycle == PrimeLifecycle::AwaitTurnEnd
                    && has_exact_keys(record, &["type", "message", "toolResults"]) =>
            {
                if record.get("message") != assistant_message.as_ref()
                    || record
                        .get("toolResults")
                        .and_then(Value::as_array)
                        .is_none_or(|results| !results.is_empty())
                {
                    return Err(malformed_because!(
                        "Prime turn_end is invalid or outside the no-tool contract.",
                    ));
                }
                lifecycle = PrimeLifecycle::AwaitAgentEnd;
            }
            Some("agent_end")
                if lifecycle == PrimeLifecycle::AwaitAgentEnd
                    && has_exact_keys(record, &["type", "messages"]) =>
            {
                let expected = [
                    user_message.as_ref().ok_or_else(|| malformed!())?,
                    assistant_message.as_ref().ok_or_else(|| malformed!())?,
                ];
                let Some(messages) = record.get("messages").and_then(Value::as_array) else {
                    return Err(malformed_because!(
                        "Prime agent_end is invalid or non-terminal.",
                    ));
                };
                if messages.len() != expected.len()
                    || messages
                        .iter()
                        .zip(expected)
                        .any(|(actual, expected)| actual != expected)
                {
                    return Err(malformed_because!(
                        "Prime agent_end is invalid or non-terminal.",
                    ));
                }
                lifecycle = PrimeLifecycle::Complete;
            }
            _ => {
                let reason = match record_type(record) {
                    Some("agent_start" | "turn_start") => {
                        "Prime lifecycle start record is duplicate or out of order."
                    }
                    Some("message_start") => "Prime message_start role or ordering is invalid.",
                    Some("message_end") => "Prime message_end role or ordering is invalid.",
                    Some("turn_end") => {
                        "Prime turn_end is invalid or outside the no-tool contract."
                    }
                    Some("agent_end") => "Prime agent_end is invalid or non-terminal.",
                    _ => "Prime emitted a flow outside the no-tool lifecycle.",
                };
                return Err(malformed_because!(reason));
            }
        }
        counters.accept(record);
    }
    if lifecycle != PrimeLifecycle::Complete {
        return Err(malformed_because!(
            "Prime stream ended before the complete no-tool lifecycle.",
        ));
    }
    Ok(TransportNormalization {
        terminal_event: "agent_end",
        assistant_messages: vec![assistant_text],
    })
}

pub(crate) fn prime_parser_diagnostic(
    records: &[Value],
    expected_rpc_id: &str,
    framing: bool,
) -> Value {
    let normalized = normalize_prime_rpc(records, expected_rpc_id);
    let mut diagnostic = normalized
        .as_ref()
        .err()
        .and_then(|error| error.details.as_ref())
        .and_then(|details| details.get("adapterDiagnostic"))
        .cloned()
        .unwrap_or_else(|| {
            let mut counters = PrimeDiagnosticCounters::default();
            for record in records {
                counters.accept(record);
            }
            let lifecycle = if normalized.is_ok() {
                PrimeLifecycle::Complete
            } else {
                PrimeLifecycle::AwaitPromptAck
            };
            prime_adapter_diagnostic(lifecycle, counters, false)
        });
    let lifecycle_accepted = diagnostic
        .pointer("/counters/acceptedRecords")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok());
    if framing && lifecycle_accepted == Some(records.len()) {
        diagnostic["stage"] = json!("jsonl-framing");
        diagnostic["phase"] = json!("record-boundary");
    }
    diagnostic
}

fn prime_bounded_json(value: &Value, depth: usize) -> bool {
    if depth > PRIME_RPC_NESTING_LIMIT {
        return false;
    }
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => true,
        Value::String(value) => value.len() <= PRIME_RPC_STRING_BYTES_LIMIT,
        Value::Array(values) => {
            values.len() <= PRIME_RPC_COLLECTION_LIMIT
                && values
                    .iter()
                    .all(|value| prime_bounded_json(value, depth + 1))
        }
        Value::Object(values) => {
            values.len() <= PRIME_RPC_COLLECTION_LIMIT
                && values.iter().all(|(key, value)| {
                    key.len() <= PRIME_RPC_STRING_BYTES_LIMIT
                        && prime_bounded_json(value, depth + 1)
                })
        }
    }
}

fn valid_prime_user_message(message: &Value) -> bool {
    let block = message
        .get("content")
        .and_then(Value::as_array)
        .filter(|content| content.len() == 1)
        .and_then(|content| content.first());
    has_exact_keys(message, &["role", "content", "timestamp"])
        && message.get("role").and_then(Value::as_str) == Some("user")
        && block.is_some_and(|block| {
            has_exact_keys(block, &["type", "text"])
                && block.get("type").and_then(Value::as_str) == Some("text")
                && block
                    .get("text")
                    .and_then(Value::as_str)
                    .is_some_and(|text| {
                        !text.is_empty() && text.len() <= PRIME_RPC_STRING_BYTES_LIMIT
                    })
        })
        && message.get("timestamp").is_some_and(nonnegative_integer)
}

fn valid_prime_assistant_message(message: &Value) -> bool {
    const ALLOWED_FIELDS: &[&str] = &[
        "role",
        "content",
        "api",
        "provider",
        "model",
        "responseModel",
        "responseId",
        "diagnostics",
        "usage",
        "stopReason",
        "stopReasonRaw",
        "errorMessage",
        "timestamp",
    ];
    const REQUIRED_FIELDS: &[&str] = &[
        "role",
        "content",
        "api",
        "provider",
        "model",
        "usage",
        "stopReason",
        "timestamp",
    ];
    let Some(object) = message.as_object() else {
        return false;
    };
    if object
        .keys()
        .any(|key| !ALLOWED_FIELDS.contains(&key.as_str()))
        || REQUIRED_FIELDS
            .iter()
            .any(|field| !object.contains_key(*field))
        || message.get("role").and_then(Value::as_str) != Some("assistant")
        || message.get("stopReason").and_then(Value::as_str) != Some("stop")
        || !["api", "provider", "model"].iter().all(|field| {
            message
                .get(*field)
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty())
        })
        || ![
            "responseModel",
            "responseId",
            "stopReasonRaw",
            "errorMessage",
        ]
        .iter()
        .all(|field| message.get(*field).is_none_or(Value::is_string))
        || message
            .get("timestamp")
            .is_none_or(|value| !nonnegative_integer(value))
        || !valid_prime_usage(message.get("usage").unwrap_or(&Value::Null))
    {
        return false;
    }
    let Some(content) = message.get("content").and_then(Value::as_array) else {
        return false;
    };
    content.len() <= PRIME_RPC_COLLECTION_LIMIT
        && content.iter().all(valid_prime_assistant_content)
        && message
            .get("diagnostics")
            .is_none_or(|value| value.is_array() && prime_bounded_json(value, 0))
}

fn valid_prime_assistant_content(content: &Value) -> bool {
    match content.get("type").and_then(Value::as_str) {
        Some("text") => {
            let Some(object) = content.as_object() else {
                return false;
            };
            object
                .keys()
                .all(|key| matches!(key.as_str(), "type" | "text" | "textSignature"))
                && content.get("text").is_some_and(Value::is_string)
                && content.get("textSignature").is_none_or(Value::is_string)
        }
        Some("thinking") => {
            let Some(object) = content.as_object() else {
                return false;
            };
            object.keys().all(|key| {
                matches!(
                    key.as_str(),
                    "type" | "thinking" | "thinkingSignature" | "redacted"
                )
            }) && content.get("thinking").is_some_and(Value::is_string)
                && content
                    .get("thinkingSignature")
                    .is_none_or(Value::is_string)
                && content.get("redacted").is_none_or(Value::is_boolean)
        }
        _ => false,
    }
}

fn valid_prime_usage(usage: &Value) -> bool {
    if !has_exact_keys(
        usage,
        &[
            "input",
            "output",
            "cacheRead",
            "cacheWrite",
            "totalTokens",
            "cost",
        ],
    ) {
        return false;
    }
    let Some(cost) = usage.get("cost") else {
        return false;
    };
    ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]
        .iter()
        .all(|field| usage.get(*field).is_some_and(nonnegative_number))
        && has_exact_keys(
            cost,
            &["input", "output", "cacheRead", "cacheWrite", "total"],
        )
        && cost
            .as_object()
            .is_some_and(|object| object.values().all(nonnegative_number))
}

fn prime_content_has_shape(message: &Value, shape: PrimeContentShape) -> bool {
    let Some(content) = message.get("content").and_then(Value::as_array) else {
        return false;
    };
    match shape {
        PrimeContentShape::Empty => content.is_empty(),
        PrimeContentShape::EmptyText => {
            content.len() == 1
                && content[0].get("type").and_then(Value::as_str) == Some("text")
                && content[0].get("text").and_then(Value::as_str) == Some("")
        }
        PrimeContentShape::Text => {
            content.len() == 1 && content[0].get("type").and_then(Value::as_str) == Some("text")
        }
        PrimeContentShape::ThinkingEmpty => {
            content.len() == 1
                && content[0].get("type").and_then(Value::as_str) == Some("thinking")
                && content[0].get("thinking").and_then(Value::as_str) == Some("")
        }
        PrimeContentShape::Thinking => {
            content.len() == 1 && content[0].get("type").and_then(Value::as_str) == Some("thinking")
        }
        PrimeContentShape::ThinkingAndEmptyText => {
            content.len() == 2
                && content[0].get("type").and_then(Value::as_str) == Some("thinking")
                && content[1].get("type").and_then(Value::as_str) == Some("text")
                && content[1].get("text").and_then(Value::as_str) == Some("")
        }
        PrimeContentShape::ThinkingAndText => {
            content.len() == 2
                && content[0].get("type").and_then(Value::as_str) == Some("thinking")
                && content[1].get("type").and_then(Value::as_str) == Some("text")
        }
    }
}

fn prime_content_has_text_shape(message: &Value, text_index: usize, empty: bool) -> bool {
    let shape = match (text_index, empty) {
        (0, true) => PrimeContentShape::EmptyText,
        (0, false) => PrimeContentShape::Text,
        (1, true) => PrimeContentShape::ThinkingAndEmptyText,
        (1, false) => PrimeContentShape::ThinkingAndText,
        _ => return false,
    };
    prime_content_has_shape(message, shape)
}

fn prime_assistant_text(message: &Value, text_index: usize) -> Option<&str> {
    prime_content_has_text_shape(message, text_index, false)
        .then(|| {
            message
                .get("content")
                .and_then(Value::as_array)
                .and_then(|content| content.get(text_index))
                .and_then(|block| block.get("text"))
                .and_then(Value::as_str)
        })
        .flatten()
}

fn prime_text_index(message: &Value) -> Option<usize> {
    [0, 1]
        .into_iter()
        .find(|index| prime_content_has_text_shape(message, *index, false))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OmpLifecycle {
    AwaitAgentStart,
    AwaitTurnStart,
    AwaitUserMessageStart,
    AwaitUserMessageEnd,
    AwaitAssistantMessageStart,
    AwaitAssistantMessageEnd,
    AwaitTurnEnd,
    AwaitAgentEnd,
    Complete,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OmpExtensionUiDisposition {
    Presentation,
    Blocked,
    Malformed,
}

fn normalize_omp_rpc(
    records: &[Value],
    expected_rpc_id: &str,
) -> Result<TransportNormalization, RunnerError> {
    if records.iter().any(native_tools::rich) { return native_tools::normalize(records, expected_rpc_id, true, true); }
    let malformed = || RunnerError::catalog(ErrorCode::ProtocolMalformed);
    let failed = || RunnerError::catalog(ErrorCode::HarnessFailed);
    let expected_state_id = omp_rpc_id(expected_rpc_id, "state.1");
    let expected_prompt_id = omp_rpc_id(expected_rpc_id, "prompt.1");
    let Some(ready) = records.first().filter(|record| valid_omp_ready(record)) else {
        return Err(malformed());
    };
    let _ = ready;

    let mut lifecycle = OmpLifecycle::AwaitAgentStart;
    let mut response_seen = false;
    let mut commands_seen = false;
    let mut state_seen = false;
    let mut assistant_messages = Vec::new();
    for record in records.iter().skip(1) {
        if record_type(record) == Some("extension_ui_request") {
            match omp_extension_ui_disposition(record) {
                OmpExtensionUiDisposition::Presentation => continue,
                OmpExtensionUiDisposition::Blocked => return Err(failed()),
                OmpExtensionUiDisposition::Malformed => return Err(malformed()),
            }
        }
        if !commands_seen && record_type(record) != Some("available_commands_update") {
            return Err(malformed());
        }
        match record_type(record) {
            Some("available_commands_update")
                if !commands_seen && lifecycle == OmpLifecycle::AwaitAgentStart =>
            {
                if !has_exact_keys(record, &["type", "commands"])
                    || !record.get("commands").is_some_and(Value::is_array)
                {
                    return Err(malformed());
                }
                commands_seen = true;
            }
            Some("response") => {
                let id = record.get("id").and_then(Value::as_str);
                let command = record.get("command").and_then(Value::as_str);
                if id == Some(expected_state_id.as_str()) && command == Some("get_state") {
                    if state_seen || lifecycle != OmpLifecycle::AwaitAgentStart {
                        return Err(malformed());
                    }
                    if record.get("success").and_then(Value::as_bool) == Some(false) {
                        return Err(failed());
                    }
                    if !has_exact_keys(record, &["id", "type", "command", "success", "data"])
                        || record.get("success").and_then(Value::as_bool) != Some(true)
                    {
                        return Err(malformed());
                    }
                    let Some(tools) = record.pointer("/data/dumpTools").and_then(Value::as_array)
                    else {
                        return Err(malformed());
                    };
                    if !tools.is_empty() {
                        return Err(failed());
                    }
                    state_seen = true;
                } else {
                    if !state_seen
                        || !has_exact_keys(record, &["id", "type", "command", "success"])
                        || response_seen
                        || id != Some(expected_prompt_id.as_str())
                        || command != Some("prompt")
                    {
                        return Err(malformed());
                    }
                    response_seen = true;
                    if record.get("success").and_then(Value::as_bool) != Some(true) {
                        return Err(failed());
                    }
                }
            }
            Some("agent_start")
                if state_seen
                    && lifecycle == OmpLifecycle::AwaitAgentStart
                    && has_exact_keys(record, &["type"]) =>
            {
                lifecycle = OmpLifecycle::AwaitTurnStart;
            }
            Some("turn_start")
                if lifecycle == OmpLifecycle::AwaitTurnStart
                    && has_exact_keys(record, &["type"]) =>
            {
                lifecycle = OmpLifecycle::AwaitUserMessageStart;
            }
            Some("message_start")
                if lifecycle == OmpLifecycle::AwaitUserMessageStart
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                require_message_role(record, "user")?;
                lifecycle = OmpLifecycle::AwaitUserMessageEnd;
            }
            Some("message_end")
                if lifecycle == OmpLifecycle::AwaitUserMessageEnd
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                require_message_role(record, "user")?;
                lifecycle = OmpLifecycle::AwaitAssistantMessageStart;
            }
            Some("message_start")
                if lifecycle == OmpLifecycle::AwaitAssistantMessageStart
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                require_message_role(record, "assistant")?;
                lifecycle = OmpLifecycle::AwaitAssistantMessageEnd;
            }
            Some("message_update") if lifecycle == OmpLifecycle::AwaitAssistantMessageEnd => {
                if !has_exact_keys(record, &["type", "assistantMessageEvent", "message"])
                    || record
                        .get("assistantMessageEvent")
                        .and_then(Value::as_object)
                        .and_then(|event| event.get("type"))
                        .and_then(Value::as_str)
                        .is_none_or(|event| !OMP_ASSISTANT_MESSAGE_EVENTS.contains(&event))
                {
                    return Err(malformed());
                }
                require_message_role(record, "assistant")?;
            }
            Some("message_end")
                if lifecycle == OmpLifecycle::AwaitAssistantMessageEnd
                    && has_exact_keys(record, &["type", "message"]) =>
            {
                assistant_messages.extend(assistant_text_content(record)?);
                lifecycle = OmpLifecycle::AwaitTurnEnd;
            }
            Some("turn_end") if lifecycle == OmpLifecycle::AwaitTurnEnd => {
                if !has_exact_keys(record, &["type", "message", "toolResults"])
                    || record
                        .get("toolResults")
                        .and_then(Value::as_array)
                        .is_none_or(|results| !results.is_empty())
                {
                    return Err(malformed());
                }
                require_message_role(record, "assistant")?;
                lifecycle = OmpLifecycle::AwaitAgentEnd;
            }
            Some("agent_end") if lifecycle == OmpLifecycle::AwaitAgentEnd => {
                match record.get("isTerminal").and_then(Value::as_bool) {
                    Some(false) => {
                        return Err(
                            failed().with_detail("reason", "unsupported_nonterminal_settlement")
                        );
                    }
                    Some(true) => {}
                    None => return Err(malformed()),
                }
                if !valid_omp_agent_end(record) {
                    return Err(malformed());
                }
                lifecycle = OmpLifecycle::Complete;
            }
            _ => return Err(malformed()),
        }
    }
    if !commands_seen || !state_seen || !response_seen || lifecycle != OmpLifecycle::Complete {
        return Err(malformed());
    }
    Ok(TransportNormalization {
        terminal_event: "agent_end",
        assistant_messages,
    })
}

pub(crate) fn omp_extension_ui_disposition(record: &Value) -> OmpExtensionUiDisposition {
    const BLOCKED_METHODS: &[&str] =
        &["select", "confirm", "input", "editor", "open_url", "cancel"];
    let Some(object) = record.as_object() else {
        return OmpExtensionUiDisposition::Malformed;
    };
    let Some(method) = record.get("method").and_then(Value::as_str) else {
        return OmpExtensionUiDisposition::Malformed;
    };
    if !matches!(
        method,
        "notify" | "setStatus" | "setWidget" | "setTitle" | "set_editor_text"
    ) {
        return if BLOCKED_METHODS.contains(&method) {
            OmpExtensionUiDisposition::Blocked
        } else {
            OmpExtensionUiDisposition::Malformed
        };
    }
    if record
        .get("id")
        .and_then(Value::as_str)
        .is_none_or(|id| id.is_empty() || !bounded_string(id))
    {
        return OmpExtensionUiDisposition::Malformed;
    }
    let allowed_fields: &[&str] = match method {
        "notify" => &["type", "id", "method", "message", "notifyType"],
        "setStatus" => &["type", "id", "method", "statusKey", "statusText"],
        "setWidget" => &[
            "type",
            "id",
            "method",
            "widgetKey",
            "widgetLines",
            "widgetPlacement",
        ],
        "setTitle" => &["type", "id", "method", "title"],
        "set_editor_text" => &["type", "id", "method", "text"],
        _ => unreachable!(),
    };
    if object
        .keys()
        .any(|key| !allowed_fields.contains(&key.as_str()))
    {
        return OmpExtensionUiDisposition::Malformed;
    }
    let required_string = match method {
        "notify" => "message",
        "setStatus" => "statusKey",
        "setWidget" => "widgetKey",
        "setTitle" => "title",
        "set_editor_text" => "text",
        _ => unreachable!(),
    };
    if record
        .get(required_string)
        .and_then(Value::as_str)
        .is_none_or(|value| !bounded_string(value))
    {
        return OmpExtensionUiDisposition::Malformed;
    }
    if method == "notify"
        && record
            .get("notifyType")
            .is_some_and(|value| !matches!(value.as_str(), Some("info" | "warning" | "error")))
    {
        return OmpExtensionUiDisposition::Malformed;
    }
    if method == "setStatus"
        && record
            .get("statusText")
            .is_some_and(|value| value.as_str().is_none_or(|text| !bounded_string(text)))
    {
        return OmpExtensionUiDisposition::Malformed;
    }
    if method != "setWidget" {
        return OmpExtensionUiDisposition::Presentation;
    }
    if let Some(lines) = record.get("widgetLines") {
        let Some(lines) = lines.as_array() else {
            return OmpExtensionUiDisposition::Malformed;
        };
        if lines.len() > OMP_TERMINAL_COLLECTION_LIMIT
            || lines
                .iter()
                .any(|line| line.as_str().is_none_or(|line| !bounded_string(line)))
        {
            return OmpExtensionUiDisposition::Malformed;
        }
    }
    if record
        .get("widgetPlacement")
        .is_some_and(|placement| !matches!(placement.as_str(), Some("aboveEditor" | "belowEditor")))
    {
        return OmpExtensionUiDisposition::Malformed;
    }
    OmpExtensionUiDisposition::Presentation
}

fn valid_omp_ready(record: &Value) -> bool {
    let Some(object) = record.as_object() else {
        return false;
    };
    if object.len() == 1 {
        return record_type(record) == Some("ready");
    }
    object.len() == 5
        && record_type(record) == Some("ready")
        && record.get("protocolVersion").and_then(Value::as_u64) == Some(1)
        && record
            .get("supportedProtocolVersions")
            .and_then(Value::as_array)
            == Some(&vec![Value::from(1), Value::from(2)])
        && record.get("maxFrameBytes").and_then(Value::as_u64) == Some(1_048_576)
        && record
            .get("maxReassembledFrameBytes")
            .and_then(Value::as_u64)
            == Some(67_108_864)
}

fn has_exact_keys(record: &Value, expected: &[&str]) -> bool {
    record.as_object().is_some_and(|object| {
        object.len() == expected.len() && expected.iter().all(|key| object.contains_key(*key))
    })
}

fn valid_omp_agent_end(record: &Value) -> bool {
    let Some(object) = record.as_object() else {
        return false;
    };
    if object
        .keys()
        .any(|key| !OMP_AGENT_END_FIELDS.contains(&key.as_str()))
    {
        return false;
    }
    let Some(messages) = record.get("messages").and_then(Value::as_array) else {
        return false;
    };
    if record.get("isTerminal").and_then(Value::as_bool) != Some(true) {
        return false;
    }
    if let Some(message_count) = record.get("messageCount") {
        let Some(message_count) = message_count.as_u64() else {
            return false;
        };
        if usize::try_from(message_count).map_or(true, |count| count < messages.len()) {
            return false;
        }
    }
    let telemetry = record.get("telemetry");
    let coverage = record.get("coverage");
    telemetry.is_some() == coverage.is_some()
        && telemetry.is_none_or(valid_omp_telemetry)
        && coverage.is_none_or(valid_omp_coverage)
}

fn valid_omp_telemetry(value: &Value) -> bool {
    if !has_exact_keys(
        value,
        &["chats", "tools", "usage", "cost", "errors", "stepCount"],
    ) {
        return false;
    }
    let Some(chats) = value.get("chats") else {
        return false;
    };
    let Some(tools) = value.get("tools") else {
        return false;
    };
    let Some(usage) = value.get("usage") else {
        return false;
    };
    let Some(cost) = value.get("cost") else {
        return false;
    };
    let Some(errors) = value.get("errors") else {
        return false;
    };

    has_exact_keys(chats, &["total", "byStopReason", "totalLatencyMs"])
        && chats.get("total").is_some_and(nonnegative_integer)
        && chats.get("totalLatencyMs").is_some_and(nonnegative_number)
        && chats.get("byStopReason").is_some_and(valid_counter_map)
        && has_exact_keys(
            tools,
            &[
                "total",
                "ok",
                "error",
                "skipped",
                "blocked",
                "timeout",
                "aborted",
                "totalLatencyMs",
                "byName",
            ],
        )
        && OMP_COUNTER_FIELDS
            .iter()
            .all(|field| tools.get(field).is_some_and(nonnegative_integer))
        && tools.get("totalLatencyMs").is_some_and(nonnegative_number)
        && tools.get("byName").is_some_and(valid_tool_counter_map)
        && has_exact_keys(
            usage,
            &[
                "inputTokens",
                "outputTokens",
                "cachedInputTokens",
                "cacheWriteTokens",
                "reasoningOutputTokens",
                "totalTokens",
            ],
        )
        && usage
            .as_object()
            .is_some_and(|object| object.values().all(nonnegative_integer))
        && has_exact_keys(cost, &["estimatedUsd", "unavailableReasons"])
        && cost.get("estimatedUsd").is_some_and(nonnegative_number)
        && cost
            .get("unavailableReasons")
            .is_some_and(valid_sorted_string_list)
        && has_exact_keys(errors, &["total", "byType"])
        && errors.get("total").is_some_and(nonnegative_integer)
        && errors.get("byType").is_some_and(valid_counter_map)
        && value.get("stepCount").is_some_and(nonnegative_integer)
}

fn valid_omp_coverage(value: &Value) -> bool {
    has_exact_keys(
        value,
        &[
            "toolsAvailable",
            "toolsInvoked",
            "toolsUnused",
            "modelsUsed",
            "providersUsed",
        ],
    ) && value
        .as_object()
        .is_some_and(|object| object.values().all(valid_sorted_string_list))
}

fn valid_tool_counter_map(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    object.len() <= OMP_TERMINAL_COLLECTION_LIMIT
        && object.iter().all(|(key, counters)| {
            bounded_string(key)
                && has_exact_keys(
                    counters,
                    &[
                        "total",
                        "ok",
                        "error",
                        "skipped",
                        "blocked",
                        "timeout",
                        "aborted",
                        "totalLatencyMs",
                    ],
                )
                && OMP_COUNTER_FIELDS
                    .iter()
                    .all(|field| counters.get(field).is_some_and(nonnegative_integer))
                && counters
                    .get("totalLatencyMs")
                    .is_some_and(nonnegative_number)
        })
}

fn valid_counter_map(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    object.len() <= OMP_TERMINAL_COLLECTION_LIMIT
        && object
            .iter()
            .all(|(key, count)| bounded_string(key) && nonnegative_integer(count))
}

fn valid_sorted_string_list(value: &Value) -> bool {
    let Some(items) = value.as_array() else {
        return false;
    };
    if items.len() > OMP_TERMINAL_COLLECTION_LIMIT {
        return false;
    }
    let mut previous: Option<&str> = None;
    for item in items {
        let Some(item) = item.as_str() else {
            return false;
        };
        if !bounded_string(item) || previous.is_some_and(|previous| previous >= item) {
            return false;
        }
        previous = Some(item);
    }
    true
}

fn bounded_string(value: &str) -> bool {
    value.len() <= OMP_TERMINAL_STRING_BYTES_LIMIT
}

fn nonnegative_integer(value: &Value) -> bool {
    value.as_u64().is_some()
}

fn nonnegative_number(value: &Value) -> bool {
    value
        .as_f64()
        .is_some_and(|number| number.is_finite() && number >= 0.0)
}

fn require_message_role(record: &Value, expected: &str) -> Result<(), RunnerError> {
    (record.pointer("/message/role").and_then(Value::as_str) == Some(expected))
        .then_some(())
        .ok_or_else(|| RunnerError::catalog(ErrorCode::ProtocolMalformed))
}

fn record_type(record: &Value) -> Option<&str> {
    record.get("type")?.as_str()
}

fn assistant_text_content(record: &Value) -> Result<Vec<String>, RunnerError> {
    let malformed = || RunnerError::catalog(ErrorCode::ProtocolMalformed);
    let message = record.get("message").unwrap_or(record);
    let role = message
        .get("role")
        .and_then(Value::as_str)
        .ok_or_else(malformed)?;
    let content = message
        .get("content")
        .and_then(Value::as_array)
        .ok_or_else(malformed)?;
    if role != "assistant" {
        return Err(malformed());
    }
    let mut texts = Vec::new();
    for item in content {
        let item_type = item
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(malformed)?;
        if item_type == "text" {
            texts.push(
                item.get("text")
                    .and_then(Value::as_str)
                    .ok_or_else(malformed)?
                    .to_owned(),
            );
        }
    }
    Ok(texts)
}

/// Recovers the final image-declared terminal object from assistant text.
///
/// The image schema must use the supported closed structural subset. The last
/// nonblank assistant line must decode to exactly that object. When the schema
/// requires `task.argv`, the value must also equal the runner-owned task argv.
/// Earlier assistant text remains opaque and is returned only for user-facing
/// display.
///
/// # Errors
///
/// Returns `IMAGE_INVALID` when the image does not declare a closed constant
/// terminal and `PROTOCOL_MALFORMED` when assistant output omits or mutates it.
pub fn recover_terminal(
    image: &RuntimeImage,
    assistant_messages: &[String],
    expected_argv: &[String],
) -> Result<RecoveredTerminal, RunnerError> {
    let malformed =
        |reason| RunnerError::catalog(ErrorCode::ProtocolMalformed).with_detail("reason", reason);
    let carrier_index = assistant_messages
        .iter()
        .rposition(|message| message.lines().any(|line| !line.trim().is_empty()))
        .ok_or_else(|| {
            malformed(
                "The harness completed without an assistant message containing the image terminal envelope.",
            )
        })?;
    let mut visible_messages = assistant_messages.to_vec();
    let mut carrier_lines = visible_messages[carrier_index]
        .split('\n')
        .collect::<Vec<_>>();
    let terminal_index = carrier_lines
        .iter()
        .rposition(|line| !line.trim().is_empty())
        .ok_or_else(|| {
            malformed(
                "The harness completed without an assistant message containing the image terminal envelope.",
            )
        })?;
    let terminal_line = carrier_lines[terminal_index];
    let observed: Value = serde_json::from_str(terminal_line).map_err(|_| {
        malformed("The final nonblank assistant line was not the image terminal envelope.")
    })?;
    if !is_exact_minified_json(terminal_line) {
        return Err(malformed(
            "The final assistant terminal envelope is not one exact minified JSON object.",
        ));
    }
    let schema: Value = serde_json::from_slice(&image.terminal_envelope_schema)
        .map_err(|_| terminal_schema_error())?;
    validate_supported_schema(&schema).map_err(|()| terminal_schema_error())?;
    validate_schema_value(&observed, &schema).map_err(|()| {
        malformed(
            "The final assistant terminal envelope does not match the verified image-declared schema.",
        )
    })?;
    let schema_requires_task_argv = required_property_schema(&schema, "task")
        .is_some_and(|task_schema| required_property_schema(task_schema, "argv").is_some());
    if observed.get("schema").and_then(Value::as_str)
        != Some(image.manifest.terminal_envelope.schema_id.as_str())
    {
        return Err(malformed(
            "The final assistant terminal identity does not match the verified image manifest.",
        ));
    }
    if schema_requires_task_argv
        && observed
            .pointer("/task/argv")
            .and_then(Value::as_array)
            .is_none_or(|argv| argv != expected_argv)
    {
        return Err(malformed(
            "The final assistant terminal does not echo the runner-owned task identity.",
        ));
    }
    carrier_lines.remove(terminal_index);
    while carrier_lines
        .last()
        .is_some_and(|line| line.trim().is_empty())
    {
        carrier_lines.pop();
    }
    visible_messages[carrier_index] = carrier_lines.join("\n");
    visible_messages.retain(|message| !message.is_empty());
    let visible_text = visible_messages.join("\n");
    Ok(RecoveredTerminal {
        envelope: observed,
        visible_messages,
        visible_text,
    })
}

/// A JSON value that preserves object insertion order while it is decoded and
/// re-encoded. Comparing this encoding with the original line mirrors
/// `JSON.stringify(JSON.parse(line))`: insignificant whitespace, alternate
/// escapes, duplicate keys, prefixes, and suffixes cannot masquerade as the
/// exact minified terminal object.
#[derive(Debug)]
enum OrderedJson {
    Null,
    Bool(bool),
    Number(serde_json::Number),
    String(String),
    Array(Vec<Self>),
    Object(Vec<(String, Self)>),
}

impl<'de> Deserialize<'de> for OrderedJson {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_any(OrderedJsonVisitor)
    }
}

struct OrderedJsonVisitor;

impl<'de> Visitor<'de> for OrderedJsonVisitor {
    type Value = OrderedJson;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a JSON value")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
        Ok(OrderedJson::Bool(value))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
        Ok(OrderedJson::Number(value.into()))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
        Ok(OrderedJson::Number(value.into()))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: serde::de::Error,
    {
        serde_json::Number::from_f64(value)
            .map(OrderedJson::Number)
            .ok_or_else(|| E::custom("JSON number is not finite"))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E> {
        Ok(OrderedJson::String(value.to_owned()))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(OrderedJson::String(value))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(OrderedJson::Null)
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(OrderedJson::Null)
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut values = Vec::with_capacity(sequence.size_hint().unwrap_or(0));
        while let Some(value) = sequence.next_element()? {
            values.push(value);
        }
        Ok(OrderedJson::Array(values))
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut values: Vec<(String, OrderedJson)> =
            Vec::with_capacity(map.size_hint().unwrap_or(0));
        while let Some((key, value)) = map.next_entry::<String, OrderedJson>()? {
            if values
                .iter()
                .any(|(existing, _)| existing.as_str() == key.as_str())
            {
                return Err(A::Error::custom("duplicate JSON object key"));
            }
            values.push((key, value));
        }
        Ok(OrderedJson::Object(values))
    }
}

impl Serialize for OrderedJson {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match self {
            Self::Null => serializer.serialize_unit(),
            Self::Bool(value) => serializer.serialize_bool(*value),
            Self::Number(value) => value.serialize(serializer),
            Self::String(value) => serializer.serialize_str(value),
            Self::Array(values) => {
                let mut sequence = serializer.serialize_seq(Some(values.len()))?;
                for value in values {
                    sequence.serialize_element(value)?;
                }
                sequence.end()
            }
            Self::Object(values) => {
                let mut map = serializer.serialize_map(Some(values.len()))?;
                for (key, value) in values {
                    map.serialize_entry(key, value)?;
                }
                map.end()
            }
        }
    }
}

fn is_exact_minified_json(line: &str) -> bool {
    serde_json::from_str::<OrderedJson>(line)
        .ok()
        .and_then(|value| serde_json::to_string(&value).ok())
        .is_some_and(|encoded| encoded == line)
}

fn terminal_schema_error() -> RunnerError {
    RunnerError::catalog(ErrorCode::ImageInvalid).with_detail(
        "reason",
        "terminal schema is outside the supported structural subset",
    )
}

fn required_property_schema<'a>(schema: &'a Value, name: &str) -> Option<&'a Value> {
    let required = schema.get("required")?.as_array()?;
    if !required
        .iter()
        .any(|required| required.as_str() == Some(name))
    {
        return None;
    }
    schema.get("properties")?.as_object()?.get(name)
}

fn validate_schema_value(value: &Value, schema: &Value) -> Result<(), ()> {
    if let Some(expected) = schema.get("const") {
        return (value == expected).then_some(()).ok_or(());
    }
    match schema.get("type").and_then(Value::as_str) {
        Some("object") => {
            let value = value.as_object().ok_or(())?;
            let properties = schema
                .get("properties")
                .and_then(Value::as_object)
                .ok_or(())?;
            let required = schema.get("required").and_then(Value::as_array).ok_or(())?;
            if schema.get("additionalProperties").and_then(Value::as_bool) != Some(false)
                || value.keys().any(|key| !properties.contains_key(key))
                || required
                    .iter()
                    .any(|key| key.as_str().is_none_or(|key| !value.contains_key(key)))
            {
                return Err(());
            }
            for (name, child) in value {
                validate_schema_value(child, properties.get(name).ok_or(())?)?;
            }
            Ok(())
        }
        Some("array") => {
            let items = schema.get("items").ok_or(())?;
            for item in value.as_array().ok_or(())? {
                validate_schema_value(item, items)?;
            }
            Ok(())
        }
        Some("string") => value.as_str().is_some().then_some(()).ok_or(()),
        Some("boolean") => value.as_bool().is_some().then_some(()).ok_or(()),
        _ => Err(()),
    }
}

fn validate_supported_schema(schema: &Value) -> Result<(), ()> {
    if schema.get("const").is_some() {
        return Ok(());
    }
    match schema.get("type").and_then(Value::as_str) {
        Some("object") => {
            let properties = schema
                .get("properties")
                .and_then(Value::as_object)
                .ok_or(())?;
            let required = schema.get("required").and_then(Value::as_array).ok_or(())?;
            if schema.get("additionalProperties").and_then(Value::as_bool) != Some(false)
                || required.iter().any(|name| {
                    name.as_str()
                        .is_none_or(|name| !properties.contains_key(name))
                })
            {
                return Err(());
            }
            properties.values().try_for_each(validate_supported_schema)
        }
        Some("array") => validate_supported_schema(schema.get("items").ok_or(())?),
        Some("string" | "boolean") => Ok(()),
        _ => Err(()),
    }
}

pub const ALL: [InstalledAdapter; 5] = [
    InstalledAdapter::AgentsSdkJsonl,
    InstalledAdapter::CodexExecJson,
    InstalledAdapter::ClaudePrintStreamJson,
    InstalledAdapter::PrimeRpc,
    InstalledAdapter::OmpRpc,
];

#[must_use]
pub fn for_harness(harness: &str) -> Option<InstalledAdapter> {
    ALL.into_iter().find(|adapter| adapter.harness() == harness)
}

/// Resolves `auto` to the frozen baseline transport and rejects every other
/// transport without fallback.
///
/// # Errors
///
/// Returns `TRANSPORT_UNSUPPORTED` for a known harness with any transport
/// other than `auto` or its frozen baseline.
pub fn select(harness: &str, transport: &str) -> Result<Option<InstalledAdapter>, RunnerError> {
    let Some(adapter) = for_harness(harness) else {
        return Ok(None);
    };
    if transport == "auto" || transport == adapter.transport() {
        return Ok(Some(adapter));
    }
    Err(RunnerError::catalog(ErrorCode::TransportUnsupported)
        .with_detail("adapterId", adapter.id())
        .with_detail("requestedTransport", transport)
        .with_detail("fallbackAttempted", false))
}

/// Finds a frozen executable name in a supplied path list. Resolution is
/// deterministic, canonical, and never consults a shell or an alias.
#[must_use]
pub fn resolve_executable(
    adapter: InstalledAdapter,
    search_path: Option<&OsStr>,
) -> Option<PathBuf> {
    resolve_named_executable(adapter.executable_names(), search_path)
}

/// Resolves the first executable for the supplied exact names from ordered
/// PATH. The first executable candidate is authoritative; callers must never
/// fall through after probing an incompatible result.
#[must_use]
pub fn resolve_named_executable(names: &[&str], search_path: Option<&OsStr>) -> Option<PathBuf> {
    let search_path = search_path?;
    for directory in std::env::split_paths(search_path).filter(|path| !path.as_os_str().is_empty())
    {
        for name in names {
            for candidate in executable_candidates(&directory, name) {
                let Ok(metadata) = fs::metadata(&candidate) else {
                    continue;
                };
                if !metadata.is_file() || !is_executable(&metadata) {
                    continue;
                }
                if let Ok(canonical) = fs::canonicalize(candidate) {
                    return Some(canonical);
                }
            }
        }
    }
    None
}

#[must_use]
pub fn runtime_prerequisite_observation(
    requirement: &RuntimePrerequisiteRequirement,
    observed: Option<&str>,
    executable_missing: bool,
) -> RuntimePrerequisiteObservation {
    let parsed = observed.and_then(parse_bun_semver);
    let availability = if executable_missing {
        "missing"
    } else if parsed.is_some_and(bun_version_admitted) {
        "available"
    } else {
        "incompatible"
    };
    RuntimePrerequisiteObservation {
        runtime: requirement.runtime,
        version_range: requirement.version_range,
        detected_version: parsed.and_then(|_| observed.map(|value| value.trim().to_owned())),
        availability,
        repair_command: requirement.repair_command,
    }
}

fn parse_bun_semver(observed: &str) -> Option<([u64; 3], bool)> {
    let observed = observed.trim();
    let (core, prerelease) = observed
        .split_once('-')
        .map_or((observed, None), |(core, suffix)| (core, Some(suffix)));
    if prerelease.is_some_and(|suffix| {
        suffix.is_empty()
            || suffix.split('.').any(str::is_empty)
            || !suffix
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
    }) {
        return None;
    }
    let mut parts = core.split('.');
    let mut parsed = [0_u64; 3];
    for slot in &mut parsed {
        let part = parts.next()?;
        if part.is_empty()
            || (part.len() > 1 && part.starts_with('0'))
            || !part.bytes().all(|byte| byte.is_ascii_digit())
        {
            return None;
        }
        *slot = part.parse().ok()?;
        if *slot > MAX_BUN_SEMVER_COMPONENT {
            return None;
        }
    }
    if parts.next().is_some() {
        return None;
    }
    Some((parsed, prerelease.is_some()))
}

fn bun_version_admitted(version: ([u64; 3], bool)) -> bool {
    !version.1 && version.0 >= [1, 3, 14]
}

#[cfg(windows)]
fn executable_candidates(directory: &Path, name: &str) -> Vec<PathBuf> {
    ["", ".exe", ".cmd", ".bat"]
        .into_iter()
        .map(|suffix| directory.join(format!("{name}{suffix}")))
        .collect()
}

#[cfg(not(windows))]
fn executable_candidates(directory: &Path, name: &str) -> Vec<PathBuf> {
    vec![directory.join(name)]
}

#[cfg(unix)]
fn is_executable(metadata: &fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt as _;
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn is_executable(_metadata: &fs::Metadata) -> bool {
    true
}

/// Constructs a closed environment for exactly one credential group.
///
/// # Errors
///
/// Returns `CONFIG_INVALID` if the named group does not belong to the adapter,
/// or `HARNESS_NEEDS_AUTH` if an environment-backed group is incomplete.
pub fn environment_policy(
    adapter: InstalledAdapter,
    auth_group: &str,
    ambient: impl IntoIterator<Item = (OsString, OsString)>,
) -> Result<EnvironmentPolicy, RunnerError> {
    let credentials = credential_names(adapter, auth_group).ok_or_else(|| {
        RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("adapterId", adapter.id())
            .with_detail(
                "reason",
                "auth profile does not select exactly one adapter credential group",
            )
    })?;
    // Remove service-only credentials from ambient storage as well as the
    // closed allowlist, so later allow_inherited calls cannot recover them.
    let ambient = ambient.into_iter()
        .filter(|(name, _)| !name.to_string_lossy().eq_ignore_ascii_case("OPENPROSE_STAGING_API_KEY"))
        .collect::<Vec<_>>();
    auth_readiness(adapter, auth_group, &ambient)?;
    let mut policy = EnvironmentPolicy::from_pairs(ambient);
    for name in BASE_ENVIRONMENT {
        policy = policy.allow_inherited(*name, Sensitivity::Public);
    }
    for name in credentials {
        policy = policy.allow_inherited(*name, Sensitivity::Secret);
    }
    Ok(policy)
}

/// Builds a public-only environment for a local `--version` probe.
///
/// A PATH-selected executable is not trusted with any credential name or
/// credential-store path until its exact version has been admitted.
pub(crate) fn version_probe_environment(
    _adapter: InstalledAdapter,
    ambient: impl IntoIterator<Item = (OsString, OsString)>,
) -> EnvironmentPolicy {
    let ambient = ambient.into_iter()
        .filter(|(name, _)| !name.to_string_lossy().eq_ignore_ascii_case("OPENPROSE_STAGING_API_KEY"));
    let mut policy = EnvironmentPolicy::from_pairs(ambient);
    for name in VERSION_PROBE_ENVIRONMENT {
        policy = policy.allow_inherited(*name, Sensitivity::Public);
    }
    policy
}

/// Reports readiness for an explicitly selected credential group without
/// contacting a provider. Harness-cache and keychain profiles are honestly
/// `unknown` (as is Claude's separately named subscription profile);
/// environment-backed profiles are also `unknown` when configured, because
/// selecting one environment group cannot prove harness-owned cache, keychain,
/// or provider routing behavior.
///
/// # Errors
///
/// Returns `CONFIG_INVALID` for a group that does not belong to the adapter,
/// or `HARNESS_NEEDS_AUTH` for an empty environment-backed group.
pub fn auth_readiness(
    adapter: InstalledAdapter,
    auth_group: &str,
    ambient: &[(OsString, OsString)],
) -> Result<&'static str, RunnerError> {
    let names = credential_names(adapter, auth_group).ok_or_else(|| {
        RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("adapterId", adapter.id())
            .with_detail(
                "reason",
                "auth profile is not supported by the selected adapter",
            )
    })?;
    if matches!(
        auth_group,
        "cached-chatgpt-login"
            | "claude-subscription"
            | "prime-harness-login"
            | "omp-harness-login"
    ) {
        return Ok("unknown");
    }
    let nonempty = |expected: &str| {
        ambient.iter().any(|(name, value)| {
            environment_name_matches(name, expected) && !value.as_os_str().is_empty()
        })
    };
    let present = match (adapter, auth_group) {
        (InstalledAdapter::CodexExecJson, "openai-api-key") => nonempty("OPENAI_API_KEY"),
        (InstalledAdapter::CodexExecJson, "codex-access-token") => nonempty("CODEX_ACCESS_TOKEN"),
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "aws-bedrock") => {
            nonempty("AWS_PROFILE")
                || (nonempty("AWS_ACCESS_KEY_ID") && nonempty("AWS_SECRET_ACCESS_KEY"))
        }
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "google") => {
            nonempty("GEMINI_API_KEY") || nonempty("GOOGLE_APPLICATION_CREDENTIALS")
        }
        _ => names.iter().any(|expected| nonempty(expected)),
    };
    if present {
        Ok("unknown")
    } else {
        Err(RunnerError::catalog(ErrorCode::HarnessNeedsAuth)
            .with_detail("adapterId", adapter.id())
            .with_detail("authProfile", auth_group))
    }
}

fn environment_name_matches(actual: &OsStr, expected: &str) -> bool {
    #[cfg(windows)]
    {
        actual.to_string_lossy().eq_ignore_ascii_case(expected)
    }
    #[cfg(not(windows))]
    {
        actual == OsStr::new(expected)
    }
}

fn credential_names(
    adapter: InstalledAdapter,
    auth_group: &str,
) -> Option<&'static [&'static str]> {
    match (adapter, auth_group) {
        (InstalledAdapter::AgentsSdkJsonl, "openai-api-key") => Some(&["OPENAI_API_KEY"]),
        (InstalledAdapter::CodexExecJson, "cached-chatgpt-login") => {
            Some(&["CODEX_HOME", "CODEX_SQLITE_HOME"])
        }
        (InstalledAdapter::CodexExecJson, "openai-api-key") => {
            Some(&["CODEX_HOME", "CODEX_SQLITE_HOME", "OPENAI_API_KEY"])
        }
        (InstalledAdapter::CodexExecJson, "codex-access-token") => {
            Some(&["CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_ACCESS_TOKEN"])
        }
        (InstalledAdapter::ClaudePrintStreamJson, "claude-subscription")
        | (InstalledAdapter::PrimeRpc, "prime-harness-login")
        | (InstalledAdapter::OmpRpc, "omp-harness-login") => Some(&[]),
        (InstalledAdapter::ClaudePrintStreamJson, "anthropic-api-key")
        | (InstalledAdapter::PrimeRpc, "anthropic") => Some(&["ANTHROPIC_API_KEY"]),
        (InstalledAdapter::OmpRpc, "anthropic") => {
            Some(&["ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN"])
        }
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "aws-bedrock") => Some(&[
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
        ]),
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "github-copilot") => {
            Some(&["GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"])
        }
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "google") => Some(&[
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
        ]),
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "openai") => {
            Some(&["OPENAI_API_KEY"])
        }
        (InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc, "openrouter") => {
            Some(&["OPENROUTER_API_KEY"])
        }
        _ => None,
    }
}

/// Builds only the adapter's mechanical launch request. Image bytes and task
/// bytes are opaque inputs; the only transformation is the image-declared v1
/// framing template or a JSON-RPC field.
///
/// # Errors
///
/// Returns an image, size, configuration, or internal-file error before spawn
/// if the opaque inputs cannot be transported exactly by the frozen recipe.
pub fn prepare_launch(
    adapter: InstalledAdapter,
    executable: PathBuf,
    cwd: &Path,
    image_bytes: &[u8],
    one_field_framing: &[u8],
    task_bytes: &[u8],
    invocation_id: &str,
    model: Option<&str>,
    auth_group: &str,
    mut environment: EnvironmentPolicy,
) -> Result<PreparedLaunch, RunnerError> {
    if adapter == InstalledAdapter::PrimeRpc && image_bytes.len() > MAX_PRIME_INLINE_IMAGE_BYTES {
        return Err(RunnerError::catalog(ErrorCode::ImageTooLarge)
            .with_detail("adapterId", adapter.id())
            .with_detail("byteLength", image_bytes.len())
            .with_detail("maximumBytes", MAX_PRIME_INLINE_IMAGE_BYTES));
    }
    let task_json = std::str::from_utf8(task_bytes).map_err(|_| {
        RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "task envelope is not canonical UTF-8")
    })?;
    if task_json.contains('\0') {
        return Err(RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "task envelope contains NUL"));
    }

    let mut prompt_files = None;
    let mut daemon_files = None;
    let mut daemon_socket_path = None;
    let mut omp_control_overlay_path = None;
    let (argv, stdin) = match adapter {
        InstalledAdapter::CodexExecJson => {
            let mut argv = vec![
                "exec".into(),
                "--skip-git-repo-check".into(),
                "--json".into(),
                "--ephemeral".into(),
                "--ignore-user-config".into(),
                "--ignore-rules".into(),
            ];
            if !cfg!(any(test, feature = "test-seams")) {
                let text = std::str::from_utf8(image_bytes).map_err(|_| RunnerError::catalog(ErrorCode::ImageInvalid))?;
                let encoded = serde_json::to_string(text).map_err(|_| RunnerError::catalog(ErrorCode::ImageInvalid))?.replace('\u{7f}', "\\u007f");
                argv.extend(["-c".into(), format!("developer_instructions={encoded}").into()]);
            }
            if auth_group == "openai-api-key" {
              let settings:Vec<String>=serde_json::from_str(include_str!("../../../../shared/capabilities/adapters/codex-env-route.v1.json")).expect("Codex API settings");
              for setting in settings {argv.extend(["-c".into(),setting.into()]);}
            }
            argv.extend(["--cd".into(), cwd.as_os_str().to_owned()]);
            append_model(&mut argv, model);
            argv.push("-".into());
            (
                argv,
                Some(if cfg!(any(test, feature = "test-seams")) { render_one_field(one_field_framing, image_bytes, task_bytes)? } else { task_bytes.to_vec() }),
            )
        }
        InstalledAdapter::AgentsSdkJsonl => {
            let files=private_files(adapter,image_bytes,task_bytes)?;
            let image_path=files.image_path().as_os_str().to_owned();prompt_files=Some(files);
            let selected=model.ok_or_else(||RunnerError::catalog(ErrorCode::ConfigInvalid).with_detail("reason","Agents SDK requires an explicit model"))?;
            (vec!["--cwd".into(),cwd.as_os_str().to_owned(),"--instructions".into(),image_path,"--model".into(),selected.into(),"--prompt".into(),task_json.into()],None)
        }
        InstalledAdapter::ClaudePrintStreamJson => {
            let files = private_files(adapter, image_bytes, task_bytes)?;
            let image_path = files.image_path().as_os_str().to_owned();
            prompt_files = Some(files);
            let mut argv = vec![
                "--safe-mode".into(),
                "--print".into(),
                "--output-format".into(),
                "stream-json".into(),
                "--verbose".into(),
                "--no-session-persistence".into(),
                "--append-system-prompt-file".into(),
                image_path,
            ];
            if auth_group == "anthropic-api-key" { argv.insert(0, "--bare".into()); }
            append_model(&mut argv, model);
            argv.push(task_json.into());
            (argv, None)
        }
        InstalledAdapter::PrimeRpc => {
            let image_text = std::str::from_utf8(image_bytes).map_err(|_| {
                RunnerError::catalog(ErrorCode::ImageInvalid).with_detail(
                    "reason",
                    "model-visible image bytes are not canonical UTF-8",
                )
            })?;
            if image_text.contains('\0') {
                return Err(RunnerError::catalog(ErrorCode::ImageInvalid)
                    .with_detail("reason", "model-visible image contains NUL"));
            }
            let files = private_daemon_files(image_bytes, task_bytes)?;
            let socket_path = files.directory().join("prime.sock");
            let mut argv = vec![
                "--mode".into(),
                "rpc".into(),
                "--no-session".into(),
                "--no-extensions".into(),
                "--no-skills".into(),
                "--no-prompt-templates".into(),
                "--no-themes".into(),
                "--no-context-files".into(),
                "--daemon-socket".into(),
                socket_path.as_os_str().to_owned(),
                "--cwd".into(),
                cwd.as_os_str().to_owned(),
                "--append-system-prompt".into(),
                image_text.into(),
            ];
            daemon_socket_path = Some(socket_path);
            daemon_files = Some(files);
            append_model(&mut argv, model);
            let stdin = match rpc_prompt(InstalledAdapter::PrimeRpc, invocation_id, task_bytes) {
                Ok(stdin) => stdin,
                Err(error) => {
                    return Err(finalize_preparation_files(
                        adapter,
                        &mut prompt_files,
                        &mut daemon_files,
                        error,
                    ));
                }
            };
            (argv, Some(stdin))
        }
        InstalledAdapter::OmpRpc => {
            let files = private_files(adapter, image_bytes, task_bytes)?;
            let image_path = files.image_path().as_os_str().to_owned();
            prompt_files = Some(files);
            let Some(omp_files) = prompt_files.as_ref() else {
                return Err(RunnerError::catalog(ErrorCode::InternalRunnerFault)
                    .with_detail("adapterId", adapter.id())
                    .with_detail("reason", "OMP launch lost its private files"));
            };
            let Ok(control_overlay_path) = omp_files.create_omp_control_overlay() else {
                let error = RunnerError::catalog(ErrorCode::InternalRunnerFault)
                    .with_detail("adapterId", adapter.id())
                    .with_detail("reason", "cannot create private OMP control overlay");
                return Err(finalize_preparation_files(
                    adapter,
                    &mut prompt_files,
                    &mut daemon_files,
                    error,
                ));
            };
            let mut argv = vec![
                "--mode".into(),
                "rpc".into(),
                "--no-session".into(),
                "--no-extensions".into(),
                "--no-skills".into(),
                "--no-rules".into(),
                "--no-lsp".into(),
                "--no-title".into(),
                "--append-system-prompt".into(),
                image_path,
            ];
            append_model(&mut argv, model);
            argv.extend([
                "--config".into(),
                control_overlay_path.as_os_str().to_owned(),
            ]);
            omp_control_overlay_path = Some(control_overlay_path);
            let stdin = match rpc_prompt(InstalledAdapter::OmpRpc, invocation_id, task_bytes) {
                Ok(stdin) => stdin,
                Err(error) => {
                    return Err(finalize_preparation_files(
                        adapter,
                        &mut prompt_files,
                        &mut daemon_files,
                        error,
                    ));
                }
            };
            (argv, Some(stdin))
        }
    };
    let credential_config = match (adapter, auth_group) {
        (InstalledAdapter::PrimeRpc, "prime-harness-login")
        | (InstalledAdapter::OmpRpc, "omp-harness-login") => None,
        (InstalledAdapter::PrimeRpc, _) => Some((
            "PRIME_AGENT_CODING_AGENT_DIR",
            daemon_files.as_ref().ok_or_else(|| {
                RunnerError::catalog(ErrorCode::InternalRunnerFault)
                    .with_detail("adapterId", adapter.id())
                    .with_detail("reason", "Prime launch lost its owned private directory")
            })?,
        )),
        (InstalledAdapter::OmpRpc, _) => Some((
            "PI_CODING_AGENT_DIR",
            prompt_files.as_ref().ok_or_else(|| {
                RunnerError::catalog(ErrorCode::InternalRunnerFault)
                    .with_detail("adapterId", adapter.id())
                    .with_detail("reason", "OMP launch lost its owned private directory")
            })?,
        )),
        _ => None,
    };
    let mut credential_config_directory = None;
    if let Some((name, files)) = credential_config {
        let Ok(directory) = files.create_credential_config_directory() else {
            let error = RunnerError::catalog(ErrorCode::InternalRunnerFault)
                .with_detail("adapterId", adapter.id())
                .with_detail(
                    "reason",
                    "cannot create private harness credential config directory",
                );
            return Err(finalize_preparation_files(
                adapter,
                &mut prompt_files,
                &mut daemon_files,
                error,
            ));
        };
        environment = environment.set(name, directory.as_os_str(), Sensitivity::Secret);
        credential_config_directory = Some(directory);
    }
    if adapter == InstalledAdapter::PrimeRpc {
        environment = environment.set(
            PRIME_AGENT_TELEMETRY_CONTROL.0,
            PRIME_AGENT_TELEMETRY_CONTROL.1,
            Sensitivity::Control,
        );
    }
    if let Err(error) = assert_argv_limits(adapter, &executable, &argv, HostPlatform::current()) {
        return Err(finalize_preparation_files(
            adapter,
            &mut prompt_files,
            &mut daemon_files,
            error,
        ));
    }
    Ok(PreparedLaunch {
        adapter,
        executable,
        argv,
        stdin,
        environment,
        prompt_files,
        daemon_files,
        daemon_socket_path,
        credential_config_directory,
        omp_control_overlay_path,
    })
}

fn finalize_preparation_files(
    adapter: InstalledAdapter,
    prompt_files: &mut Option<PrivatePromptFiles>,
    daemon_files: &mut Option<PrivatePromptFiles>,
    original_error: RunnerError,
) -> RunnerError {
    let mut cleanup_failed = false;
    for files in [prompt_files, daemon_files] {
        if let Some(mut files) = files.take() {
            cleanup_failed |= files.close().is_err();
        }
    }
    if cleanup_failed {
        private_file_cleanup_failure(adapter)
    } else {
        original_error
    }
}

fn private_daemon_files(image: &[u8], task: &[u8]) -> Result<PrivatePromptFiles, RunnerError> {
    private_daemon_files_with(|| PrivatePromptFiles::create_prime(image, task))
}

fn private_daemon_files_with(
    allocate: impl FnOnce() -> Result<PrivatePromptFiles, PrivatePromptFilesCreateError>,
) -> Result<PrivatePromptFiles, RunnerError> {
    let files = allocate().map_err(|error| {
        if error.cleanup_failed() {
            private_file_cleanup_failure(InstalledAdapter::PrimeRpc)
        } else {
            RunnerError::catalog(ErrorCode::InternalRunnerFault)
                .with_detail("adapterId", InstalledAdapter::PrimeRpc.id())
                .with_detail(
                    "reason",
                    "cannot allocate private Prime daemon socket directory",
                )
        }
    })?;
    Ok(files)
}

fn assert_argv_limits(
    adapter: InstalledAdapter,
    executable: &Path,
    argv: &[OsString],
    host: HostPlatform<'_>,
) -> Result<(), RunnerError> {
    let arguments =
        std::iter::once(executable.as_os_str()).chain(argv.iter().map(OsString::as_os_str));
    let values = arguments.collect::<Vec<_>>();
    if values.len() > MAX_ARGV_ITEMS {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("adapterId", adapter.id())
            .with_detail(
                "reason",
                "installed-adapter argv contains too many arguments",
            ));
    }
    if matches!(host.os, "windows" | "win32") {
        let mut total = 0usize;
        for argument in values {
            let text = argument.to_str().ok_or_else(|| {
                RunnerError::catalog(ErrorCode::ConfigInvalid)
                    .with_detail("adapterId", adapter.id())
                    .with_detail("reason", "installed-adapter argv is not Unicode on Windows")
            })?;
            let units = text.encode_utf16().count();
            if units > MAX_WINDOWS_ARGUMENT_UTF16_UNITS {
                return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
                    .with_detail("adapterId", adapter.id())
                    .with_detail(
                        "reason",
                        "installed-adapter argument exceeds the Windows UTF-16 limit",
                    ));
            }
            total = total.saturating_add(units.saturating_mul(2).saturating_add(3));
        }
        if total > MAX_WINDOWS_ARGV_CHARGED_UTF16_UNITS {
            return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
                .with_detail("adapterId", adapter.id())
                .with_detail(
                    "reason",
                    "installed-adapter argv exceeds the conservative Windows command-line limit",
                ));
        }
        return Ok(());
    }
    let mut total = 0usize;
    for argument in values {
        let bytes = argument.as_encoded_bytes().len();
        if bytes > MAX_POSIX_ARGUMENT_BYTES {
            return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
                .with_detail("adapterId", adapter.id())
                .with_detail(
                    "reason",
                    "installed-adapter argument exceeds the POSIX byte limit",
                ));
        }
        total = total.saturating_add(bytes.saturating_add(1));
    }
    if total > MAX_POSIX_ARGV_BYTES {
        return Err(RunnerError::catalog(ErrorCode::ConfigInvalid)
            .with_detail("adapterId", adapter.id())
            .with_detail(
                "reason",
                "installed-adapter argv exceeds the conservative POSIX byte limit",
            ));
    }
    Ok(())
}

fn append_model(argv: &mut Vec<OsString>, model: Option<&str>) {
    if let Some(model) = model {
        argv.extend(["--model".into(), model.into()]);
    }
}

fn private_files(
    adapter: InstalledAdapter,
    image: &[u8],
    task: &[u8],
) -> Result<PrivatePromptFiles, RunnerError> {
    private_files_with(adapter, || PrivatePromptFiles::create(image, task))
}

fn private_files_with(
    adapter: InstalledAdapter,
    allocate: impl FnOnce() -> Result<PrivatePromptFiles, PrivatePromptFilesCreateError>,
) -> Result<PrivatePromptFiles, RunnerError> {
    allocate().map_err(|error| {
        if error.cleanup_failed() {
            private_file_cleanup_failure(adapter)
        } else {
            RunnerError::catalog(ErrorCode::InternalRunnerFault).with_detail(
                "reason",
                "cannot create private installed-adapter input files",
            )
        }
    })
}

fn render_one_field(framing: &[u8], image: &[u8], task: &[u8]) -> Result<Vec<u8>, RunnerError> {
    let framing = std::str::from_utf8(framing).map_err(|_| {
        RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "one-field framing is not canonical UTF-8")
    })?;
    let image_text = std::str::from_utf8(image).map_err(|_| {
        RunnerError::catalog(ErrorCode::ImageInvalid).with_detail(
            "reason",
            "model-visible image bytes are not canonical UTF-8",
        )
    })?;
    let task_text = std::str::from_utf8(task).map_err(|_| {
        RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "task envelope is not canonical UTF-8")
    })?;
    if image_text.contains('\0') || task_text.contains('\0') {
        return Err(RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "one-field transport input contains NUL"));
    }
    if !framing.contains("{{IMAGE_SHA256}}")
        || !framing.contains("{{IMAGE_BYTES}}")
        || !framing.contains("{{TASK_SHA256}}")
        || !framing.contains("{{TASK_JSON}}")
    {
        return Err(RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "one-field framing omits a required placeholder"));
    }
    Ok(framing
        .replace("{{IMAGE_SHA256}}", &sha256_hex(image))
        .replace("{{IMAGE_BYTES}}", image_text)
        .replace("{{TASK_SHA256}}", &sha256_hex(task))
        .replace("{{TASK_JSON}}", task_text)
        .into_bytes())
}

fn rpc_prompt(
    adapter: InstalledAdapter,
    invocation_id: &str,
    message: &[u8],
) -> Result<Vec<u8>, RunnerError> {
    let message = std::str::from_utf8(message).map_err(|_| {
        RunnerError::catalog(ErrorCode::ImageInvalid)
            .with_detail("reason", "RPC prompt is not canonical UTF-8")
    })?;
    let mut bytes = if adapter == InstalledAdapter::PrimeRpc {
        serde_json::to_vec(&serde_json::json!({
            "id":invocation_id,
            "type":"prompt",
            "message":message
        }))
    } else {
        #[derive(Serialize)]
        struct RpcPrompt<'a> {
            id: &'a str,
            message: &'a str,
            #[serde(rename = "type")]
            record_type: &'static str,
        }
        let prompt_id = omp_rpc_id(invocation_id, "prompt.1");
        serde_json::to_vec(&RpcPrompt {
            id: &prompt_id,
            message,
            record_type: "prompt",
        })
    }
    .map_err(|_| RunnerError::catalog(ErrorCode::InternalRunnerFault))?;
    bytes.push(b'\n');
    Ok(bytes)
}

#[must_use]
pub(crate) fn omp_rpc_id(invocation_id: &str, suffix: &str) -> String {
    format!("{invocation_id}.omp.{suffix}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use tempfile::TempDir;

    const FRAMING: &[u8] =
        include_bytes!("../../../../shared/image/echo-v0/contracts/one-field-framing.txt");
    const TASK: &str = r#"{"argv":["prose","run","path with spaces/example.prose.md","--model","雪","","line\nbreak",";$(touch nope)"],"interactionMode":"non-interactive","schema":"openprose.task-envelope/1"}"#;

    fn full_image() -> Vec<u8> {
        echo_image()
            .payload
            .into_iter()
            .flat_map(|entry| entry.bytes)
            .collect()
    }

    fn echo_image() -> RuntimeImage {
        RuntimeImage::load(
            &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../shared/image/echo-v0"),
        )
        .unwrap()
    }

    fn sentinel_image() -> RuntimeImage {
        RuntimeImage::load(
            &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../shared/image/sentinel-v1"),
        )
        .unwrap()
    }

    #[test]
    fn staging_service_token_cannot_be_reallowed_into_harness_or_probe() {
        let secret = "rr_test_11111111111111111111111111111111";
        let ambient = || vec![(OsString::from("OPENPROSE_STAGING_API_KEY"), OsString::from(secret))];
        let adapter = InstalledAdapter::CodexExecJson;
        let harness = environment_policy(adapter, adapter.default_probe_auth_group(), ambient()).unwrap();
        let probe = version_probe_environment(adapter, ambient());
        for policy in [harness, probe] {
            let policy = policy.allow_inherited("OPENPROSE_STAGING_API_KEY", Sensitivity::Secret);
            assert!(!policy.secret_strings().iter().any(|value| value == secret));
            assert!(!policy.output_protected_strings().iter().any(|value| value == secret));
        }
    }

    fn empty_environment(adapter: InstalledAdapter) -> EnvironmentPolicy {
        let ambient = match adapter {
            InstalledAdapter::AgentsSdkJsonl => vec![("OPENAI_API_KEY".into(),"fixture-secret".into())],
            InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc => {
                vec![("OPENROUTER_API_KEY".into(), "fixture-secret".into())]
            }
            InstalledAdapter::CodexExecJson | InstalledAdapter::ClaudePrintStreamJson => Vec::new(),
        };
        environment_policy(adapter, adapter.default_probe_auth_group(), ambient).unwrap()
    }

    #[test]
    fn runtime_prerequisite_recipe_and_closed_bun_version_matrix_are_exact() {
        for adapter in [
            InstalledAdapter::PrimeRpc,
            InstalledAdapter::CodexExecJson,
            InstalledAdapter::ClaudePrintStreamJson,
        ] {
            assert!(
                adapter.runtime_prerequisites().is_empty(),
                "{}",
                adapter.id()
            );
        }
        let requirement = InstalledAdapter::OmpRpc
            .runtime_prerequisites()
            .into_iter()
            .next()
            .unwrap();
        assert_eq!(requirement.runtime, "bun");
        assert_eq!(requirement.version_range, ">=1.3.14");
        assert_eq!(
            requirement.repair_command,
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9"
        );

        let missing = runtime_prerequisite_observation(&requirement, None, true);
        assert_eq!(missing.availability, "missing");
        assert_eq!(missing.detected_version, None);
        for (observed, availability, detected) in [
            ("1.3.13", "incompatible", Some("1.3.13")),
            ("1.3.14", "available", Some("1.3.14")),
            ("1.3.15", "available", Some("1.3.15")),
            ("1.4.0", "available", Some("1.4.0")),
            ("2.0.0", "available", Some("2.0.0")),
            (
                "1000000.1000000.1000000",
                "available",
                Some("1000000.1000000.1000000"),
            ),
            ("1000001.3.14", "incompatible", None),
            ("1.1000001.14", "incompatible", None),
            ("1.3.1000001", "incompatible", None),
            ("1.3.14-beta.1", "incompatible", Some("1.3.14-beta.1")),
            (
                "1000000.0.0-beta.1",
                "incompatible",
                Some("1000000.0.0-beta.1"),
            ),
            ("1.3.14-beta..1", "incompatible", None),
            ("01.3.14", "incompatible", None),
            ("hostile $(touch never)", "incompatible", None),
        ] {
            let observation = runtime_prerequisite_observation(&requirement, Some(observed), false);
            assert_eq!(observation.availability, availability, "{observed}");
            assert_eq!(
                observation.detected_version.as_deref(),
                detected,
                "{observed}"
            );
            let serialized = serde_json::to_string(&observation).unwrap();
            assert!(!serialized.contains("touch never"));
            assert!(!serialized.contains("hostile"));
        }
    }

    fn strip_prime_thinking(message: &mut Value) {
        if message.get("role").and_then(Value::as_str) != Some("assistant") {
            return;
        }
        let Some(content) = message.get_mut("content").and_then(Value::as_array_mut) else {
            return;
        };
        if content.len() == 2 {
            content.remove(0);
        }
    }

    fn prime_text_only_records(records: &[Value]) -> Vec<Value> {
        let mut text_only = records[..6]
            .iter()
            .chain(records[11..].iter())
            .cloned()
            .collect::<Vec<_>>();
        for record in &mut text_only {
            if record_type(record) == Some("message_update")
                && record
                    .pointer("/assistantMessageEvent/contentIndex")
                    .and_then(Value::as_u64)
                    == Some(1)
            {
                record["assistantMessageEvent"]["contentIndex"] = json!(0);
            }
            if let Some(message) = record.get_mut("message") {
                strip_prime_thinking(message);
            }
            if let Some(messages) = record.get_mut("messages").and_then(Value::as_array_mut) {
                for message in messages {
                    strip_prime_thinking(message);
                }
            }
        }
        text_only
    }

    #[test]
    fn embedded_recipe_identity_cannot_drift_from_the_adapter() {
        for adapter in ALL {
            let recipe: Value = serde_json::from_str(adapter.recipe_json()).unwrap();
            assert_eq!(recipe["adapterId"], adapter.id());
            assert_eq!(recipe["runtime"], "installed-process");
            assert_eq!(recipe["launch"]["shell"], false);
            assert_eq!(recipe["launch"]["outerPty"], false);
            assert_eq!(recipe["admissionClaims"], serde_json::json!([]));
            let expected_stream = match adapter.version_probe().output {
                VersionProbeOutput::Stdout => "stdout",
                VersionProbeOutput::Stderr => "stderr",
            };
            assert_eq!(recipe["probe"]["versionStream"], expected_stream);
        }
        assert_eq!(
            InstalledAdapter::PrimeRpc.version_probe().output,
            VersionProbeOutput::Stderr
        );
        assert!([
            InstalledAdapter::CodexExecJson,
            InstalledAdapter::ClaudePrintStreamJson,
            InstalledAdapter::OmpRpc,
        ]
        .into_iter()
        .all(|adapter| adapter.version_probe().output == VersionProbeOutput::Stdout));
    }

    #[test]
    fn base_environment_cannot_drift_from_the_shared_cross_platform_policy() {
        let oracle: Value = serde_json::from_str(include_str!(
            "../../../../shared/capabilities/adapters/oracle.v1.json"
        ))
        .unwrap();
        let shared = oracle["baseEnvironmentAllowlist"]
            .as_array()
            .unwrap()
            .iter()
            .map(Value::as_str)
            .collect::<Option<Vec<_>>>()
            .unwrap();
        assert_eq!(BASE_ENVIRONMENT, shared);
        assert!(shared.contains(&"USER"));
        assert!(shared.contains(&"USERPROFILE"));
        let prime_controls = oracle["environmentRules"]["adapterOwnedControls"]["prime/rpc"]
            .as_object()
            .unwrap();
        assert_eq!(prime_controls.len(), 1);
        assert_eq!(
            prime_controls[PRIME_AGENT_TELEMETRY_CONTROL.0],
            PRIME_AGENT_TELEMETRY_CONTROL.1
        );
    }

    #[test]
    fn selection_is_closed_and_never_falls_back() {
        assert_eq!(
            select("codex", "auto").unwrap(),
            Some(InstalledAdapter::CodexExecJson)
        );
        let error = select("codex", "rpc").unwrap_err();
        assert_eq!(error.code, ErrorCode::TransportUnsupported);
        assert_eq!(
            error.details.unwrap().get("fallbackAttempted"),
            Some(&Value::Bool(false))
        );
        assert_eq!(select("unknown", "auto").unwrap(), None);
    }

    #[test]
    fn launch_plans_match_the_full_image_wire_fixtures() {
        let root = TempDir::new().unwrap();
        let image = full_image();
        assert_eq!(image.len(), 1310);
        assert_eq!(
            sha256_hex(&image),
            "5b10702a77d29104cc0f145b8971001e0f0d07e30de07debd09b3cba2ffb68ae"
        );
        let executable = root.path().join("adapter-probe");
        let invocation_id = "fixture-invocation-0001";
        for adapter in ALL.into_iter().filter(|adapter| *adapter != InstalledAdapter::AgentsSdkJsonl) {
            let launch = prepare_launch(
                adapter,
                executable.clone(),
                root.path(),
                &image,
                FRAMING,
                TASK.as_bytes(),
                invocation_id,
                None,
                adapter.default_probe_auth_group(),
                empty_environment(adapter),
            )
            .unwrap();
            assert_eq!(launch.adapter, adapter);
            assert_eq!(launch.executable, executable);
            match adapter {
                InstalledAdapter::AgentsSdkJsonl => unreachable!("SDK has separate native tests"),
                InstalledAdapter::CodexExecJson => assert_eq!(
                    launch.stdin.as_deref(),
                    Some(
                        &include_bytes!(
                            "../../../../shared/fixtures/adapters/wire/codex-stdin.txt"
                        )[..]
                    )
                ),
                InstalledAdapter::PrimeRpc => assert_eq!(
                    launch.stdin.as_deref(),
                    Some(
                        &include_bytes!(
                            "../../../../shared/fixtures/adapters/wire/prime-stdin.jsonl"
                        )[..]
                    )
                ),
                InstalledAdapter::OmpRpc => {
                    let wire =
                        include_bytes!("../../../../shared/fixtures/adapters/wire/omp-stdin.jsonl");
                    let prompt_offset = wire.iter().position(|byte| *byte == b'\n').unwrap() + 1;
                    assert_eq!(launch.stdin.as_deref(), Some(&wire[prompt_offset..]));
                    let overlay = launch.omp_control_overlay_path().unwrap();
                    assert_eq!(
                        fs::read(overlay).unwrap(),
                        prose_process_supervisor::OMP_CONTROL_OVERLAY_BYTES
                    );
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt as _;
                        assert_eq!(
                            fs::metadata(overlay).unwrap().permissions().mode() & 0o777,
                            0o600
                        );
                    }
                }
                InstalledAdapter::ClaudePrintStreamJson => {
                    assert!(launch.stdin.is_none());
                }
            }
            if matches!(
                adapter,
                InstalledAdapter::ClaudePrintStreamJson | InstalledAdapter::OmpRpc
            ) {
                let path = launch.image_path().unwrap();
                assert_eq!(fs::read(path).unwrap(), image);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt as _;
                    assert_eq!(
                        fs::metadata(path).unwrap().permissions().mode() & 0o777,
                        0o600
                    );
                }
            } else {
                assert!(launch.image_path().is_none());
            }
        }
    }

    #[test]
    fn model_argv_matches_shared_scenarios_at_each_harness_boundary() {
        let root = TempDir::new().unwrap();
        let image = full_image();
        let executable = root.path().join("adapter-probe");
        for (adapter, scenario_source) in [
            (
                InstalledAdapter::CodexExecJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/codex-exec-json.v1.json"
                ),
            ),
            (
                InstalledAdapter::ClaudePrintStreamJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/claude-print-stream-json.v1.json"
                ),
            ),
            (
                InstalledAdapter::PrimeRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"),
            ),
            (
                InstalledAdapter::OmpRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/omp-rpc.v1.json"),
            ),
        ] {
            let launch = prepare_launch(
                adapter,
                executable.clone(),
                root.path(),
                &image,
                FRAMING,
                TASK.as_bytes(),
                "fixture-invocation-0001",
                Some("fixture-model"),
                adapter.default_probe_auth_group(),
                empty_environment(adapter),
            )
            .unwrap();
            let scenario: Value = serde_json::from_str(scenario_source).unwrap();
            let expected = scenario["expectedArgvWithModel"]
                .as_array()
                .unwrap()
                .iter()
                .skip(1)
                .map(|value| {
                    value
                        .as_str()
                        .unwrap()
                        .replace("{{WORKSPACE}}", &root.path().display().to_string())
                        .replace("{{MODEL}}", "fixture-model")
                        .replace(
                            "{{DAEMON_SOCKET_PATH}}",
                            &launch
                                .daemon_socket_path()
                                .unwrap_or(Path::new(""))
                                .display()
                                .to_string(),
                        )
                        .replace("{{TASK_JSON}}", TASK)
                        .replace("{{IMAGE_UTF8}}", std::str::from_utf8(&image).unwrap())
                        .replace(
                            "{{IMAGE_PATH}}",
                            &launch
                                .image_path()
                                .unwrap_or(Path::new(""))
                                .display()
                                .to_string(),
                        )
                        .replace(
                            "{{RENDERED_CONFIG_PATH}}",
                            &launch
                                .omp_control_overlay_path()
                                .unwrap_or(Path::new(""))
                                .display()
                                .to_string(),
                        )
                })
                .collect::<Vec<_>>();
            let actual = launch
                .argv
                .iter()
                .map(|value| value.to_string_lossy().into_owned())
                .collect::<Vec<_>>();
            assert_eq!(actual, expected, "{}", adapter.id());
        }
    }

    #[test]
    fn prime_daemon_socket_is_unique_private_retained_and_removed_without_fallback() {
        let image = full_image();
        let make = || {
            prepare_launch(
                InstalledAdapter::PrimeRpc,
                PathBuf::from("/fixture/prime-agent"),
                Path::new("/fixture"),
                &image,
                FRAMING,
                TASK.as_bytes(),
                "fixture",
                Some("fixture-model"),
                "prime-harness-login",
                empty_environment(InstalledAdapter::PrimeRpc),
            )
            .unwrap()
        };
        let first = make();
        let second = make();
        let first_socket = first.daemon_socket_path().unwrap().to_owned();
        let second_socket = second.daemon_socket_path().unwrap().to_owned();
        assert_ne!(first_socket, second_socket);
        assert_eq!(first_socket.file_name().unwrap(), "prime.sock");
        assert!(!first_socket.exists());
        assert!(first_socket.parent().unwrap().is_dir());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                fs::metadata(first_socket.parent().unwrap())
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
        }
        let first_argv = first
            .argv
            .iter()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        let flag = first_argv
            .iter()
            .position(|value| value == "--daemon-socket")
            .unwrap();
        assert_eq!(
            first_argv
                .iter()
                .filter(|value| *value == "--daemon-socket")
                .count(),
            1
        );
        assert_eq!(first_argv[flag + 1], first_socket.display().to_string());
        assert!(!first_argv[flag + 1].contains(".prime-agent"));
        let first_directory = first_socket.parent().unwrap().to_owned();
        drop(first);
        assert!(!first_directory.exists());
        drop(second);

        assert_eq!(
            private_daemon_files_with(|| {
                Err(PrivatePromptFilesCreateError::Creation(
                    std::io::Error::new(
                        std::io::ErrorKind::PermissionDenied,
                        "fixture allocation refusal",
                    ),
                ))
            })
            .unwrap_err()
            .code,
            ErrorCode::InternalRunnerFault
        );
        assert_eq!(
            private_daemon_files_with(|| {
                Err(PrivatePromptFilesCreateError::Cleanup(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "fixture cleanup refusal",
                )))
            })
            .unwrap_err()
            .code,
            ErrorCode::ProcessCleanupFailed
        );
        for adapter in [
            InstalledAdapter::ClaudePrintStreamJson,
            InstalledAdapter::OmpRpc,
        ] {
            let error = private_files_with(adapter, || {
                Err(PrivatePromptFilesCreateError::Cleanup(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "fixture cleanup refusal",
                )))
            })
            .unwrap_err();
            assert_eq!(error.code, ErrorCode::ProcessCleanupFailed);
            assert_eq!(error.details.as_deref().unwrap()["adapterId"], adapter.id());
        }
    }

    #[test]
    fn exact_version_allowlists_reject_nearby_or_malformed_versions() {
        assert!(InstalledAdapter::CodexExecJson.version_is_supported("codex-cli 0.149.0-alpha.4.1"));
        assert!(
            !InstalledAdapter::CodexExecJson.version_is_supported("codex-cli 0.149.0-alpha.4.2")
        );
        assert!(!InstalledAdapter::CodexExecJson.version_is_supported("codex-cli 0.150.0"));
        assert!(!InstalledAdapter::CodexExecJson.version_is_supported("codex-cli 0.150.0-alpha.8"));
        assert!(!InstalledAdapter::CodexExecJson.version_is_supported("codex-cli 0.149.0-alpha.3"));
        assert!(!InstalledAdapter::CodexExecJson.version_is_supported("codex-cli 1.0.0"));
        assert!(!InstalledAdapter::CodexExecJson.version_is_supported("codex 0.149.0"));
        assert!(!InstalledAdapter::CodexExecJson.version_is_supported("0.149.0"));
        assert!(
            InstalledAdapter::ClaudePrintStreamJson.version_is_supported("2.1.243 (Claude Code)")
        );
        assert!(!InstalledAdapter::ClaudePrintStreamJson.version_is_supported("2.1.244"));
        assert!(!InstalledAdapter::ClaudePrintStreamJson.version_is_supported("2.1.242"));
        assert!(!InstalledAdapter::ClaudePrintStreamJson.version_is_supported("2.2.0"));
        assert!(!InstalledAdapter::ClaudePrintStreamJson.version_is_supported("2.2.0-alpha.1"));
        assert!(!InstalledAdapter::ClaudePrintStreamJson.version_is_supported("3.0.0"));
        assert!(!InstalledAdapter::ClaudePrintStreamJson.version_is_supported("claude 2.1.243"));
        assert!(InstalledAdapter::PrimeRpc.version_is_supported("prime-agent 0.8.1"));
        assert!(InstalledAdapter::PrimeRpc.version_is_supported("0.7.0"));
        assert!(!InstalledAdapter::PrimeRpc.version_is_supported("0.7.1"));
        assert!(!InstalledAdapter::PrimeRpc.version_is_supported("0.8.0"));
        assert!(!InstalledAdapter::PrimeRpc.version_is_supported("0.8.2"));
        assert!(!InstalledAdapter::PrimeRpc.version_is_supported("0.9.0"));
        assert!(!InstalledAdapter::PrimeRpc.version_is_supported("prime 0.8.0"));
        assert!(InstalledAdapter::OmpRpc.version_is_supported("omp/18.0.9"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("omp/18.0.8"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("omp/18.0.10"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("omp/18.9.0"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("omp/19.0.0"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("18.0.9"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("omp 18.0.9"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("oh-omp 18.0.9"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("other 18.0.9"));
        assert!(!InstalledAdapter::OmpRpc.version_is_supported("18.0.9 trailing"));
    }

    #[test]
    fn platform_admission_is_recipe_declared_and_test_injectable() {
        assert_eq!(
            assert_platform_supported(
                InstalledAdapter::CodexExecJson,
                HostPlatform {
                    os: "linux",
                    arch: "x86_64",
                    libc: Some("gnu")
                },
            )
            .unwrap(),
            "linux-x64-musl"
        );
        assert_eq!(
            assert_platform_supported(
                InstalledAdapter::OmpRpc,
                HostPlatform {
                    os: "linux",
                    arch: "x86_64",
                    libc: Some("gnu")
                },
            )
            .unwrap(),
            "linux-x64"
        );
        assert_eq!(
            assert_platform_supported(
                InstalledAdapter::ClaudePrintStreamJson,
                HostPlatform {
                    os: "linux",
                    arch: "x86_64",
                    libc: Some("gnu")
                },
            )
            .unwrap_err()
            .code,
            ErrorCode::HarnessIncompatible
        );
        assert_eq!(
            assert_platform_supported(
                InstalledAdapter::PrimeRpc,
                HostPlatform {
                    os: "macos",
                    arch: "x86_64",
                    libc: None
                },
            )
            .unwrap_err()
            .code,
            ErrorCode::HarnessIncompatible
        );
    }

    #[test]
    fn rejected_host_diagnostics_use_portable_platform_names() {
        for (os, arch, wanted_os, wanted_arch) in [
            ("linux", "aarch64", "linux", "arm64"),
            ("macos", "x86_64", "darwin", "x64"),
        ] {
            let error = assert_platform_supported(
                InstalledAdapter::PrimeRpc,
                HostPlatform { os, arch, libc: None },
            ).unwrap_err();
            let value = serde_json::to_value(error).unwrap();
            assert_eq!(value["details"]["hostPlatform"], wanted_os);
            assert_eq!(value["details"]["hostArchitecture"], wanted_arch);
            assert_eq!(value["details"]["fallbackAttempted"], false);
        }
    }

    #[test]
    fn complete_credential_alternatives_reject_routing_metadata_and_partial_keys() {
        for adapter in [InstalledAdapter::PrimeRpc, InstalledAdapter::OmpRpc] {
            for ambient in [
                vec![("AWS_REGION".into(), "us-east-1".into())],
                vec![("AWS_ACCESS_KEY_ID".into(), "access-only".into())],
                vec![("AWS_SECRET_ACCESS_KEY".into(), "secret-only".into())],
            ] {
                assert_eq!(
                    auth_readiness(adapter, "aws-bedrock", &ambient)
                        .unwrap_err()
                        .code,
                    ErrorCode::HarnessNeedsAuth
                );
            }
            assert_eq!(
                auth_readiness(
                    adapter,
                    "aws-bedrock",
                    &[("AWS_PROFILE".into(), "fixture".into())]
                )
                .unwrap(),
                "unknown"
            );
            assert_eq!(
                auth_readiness(
                    adapter,
                    "aws-bedrock",
                    &[
                        ("AWS_ACCESS_KEY_ID".into(), "access".into()),
                        ("AWS_SECRET_ACCESS_KEY".into(), "secret".into())
                    ],
                )
                .unwrap(),
                "unknown"
            );
            assert_eq!(
                auth_readiness(
                    adapter,
                    "google",
                    &[("GOOGLE_CLOUD_PROJECT".into(), "project".into())]
                )
                .unwrap_err()
                .code,
                ErrorCode::HarnessNeedsAuth
            );
            assert_eq!(
                auth_readiness(
                    adapter,
                    "google",
                    &[(
                        "GOOGLE_APPLICATION_CREDENTIALS".into(),
                        "/fixture/key.json".into()
                    )]
                )
                .unwrap(),
                "unknown"
            );
        }
        assert_eq!(
            auth_readiness(
                InstalledAdapter::CodexExecJson,
                "openai-api-key",
                &[("CODEX_HOME".into(), "/fixture/codex".into())],
            )
            .unwrap_err()
            .code,
            ErrorCode::HarnessNeedsAuth
        );
        assert_eq!(
            environment_policy(
                InstalledAdapter::PrimeRpc,
                "aws-bedrock",
                [("AWS_REGION".into(), "us-east-1".into())],
            )
            .unwrap_err()
            .code,
            ErrorCode::HarnessNeedsAuth
        );
    }

    #[test]
    fn explicit_prime_and_omp_harness_login_routes_are_probe_owned_home_backed_and_secret_free() {
        for (adapter, auth_group) in [
            (InstalledAdapter::PrimeRpc, "prime-harness-login"),
            (InstalledAdapter::OmpRpc, "omp-harness-login"),
        ] {
            assert_eq!(credential_names(adapter, auth_group), Some([].as_slice()));
            let raw_secret = "raw-provider-secret-must-not-appear";
            let ambient = [
                ("PATH", "/bin"),
                ("HOME", "/fixture/home"),
                ("USER", "fixture"),
                ("ANTHROPIC_API_KEY", raw_secret),
                ("ANTHROPIC_OAUTH_TOKEN", raw_secret),
                ("OPENAI_API_KEY", raw_secret),
                ("OPENROUTER_API_KEY", raw_secret),
                ("GEMINI_API_KEY", raw_secret),
                (
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "/fixture/provider-key.json",
                ),
                ("GOOGLE_CLOUD_PROJECT", "provider-project"),
                ("GOOGLE_CLOUD_LOCATION", "provider-location"),
                ("GITHUB_TOKEN", raw_secret),
                ("GH_TOKEN", raw_secret),
                ("COPILOT_GITHUB_TOKEN", raw_secret),
                ("AWS_ACCESS_KEY_ID", raw_secret),
                ("AWS_SECRET_ACCESS_KEY", raw_secret),
                ("AWS_SESSION_TOKEN", raw_secret),
                ("AWS_PROFILE", "provider-profile"),
                ("AWS_REGION", "provider-region"),
                ("AWS_DEFAULT_REGION", "provider-default-region"),
                (
                    "PRIME_AGENT_CODING_AGENT_DIR",
                    "/fixture/prime-store-override",
                ),
                ("PI_CODING_AGENT_DIR", "/fixture/omp-store-override"),
                ("PRIME_AGENT_TELEMETRY", "1"),
            ];
            let ambient = ambient
                .into_iter()
                .map(|(name, value)| (OsString::from(name), OsString::from(value)))
                .collect::<Vec<_>>();
            assert_eq!(
                auth_readiness(adapter, auth_group, &ambient).unwrap(),
                "unknown"
            );
            let policy = environment_policy(adapter, auth_group, ambient).unwrap();
            let debug = format!("{policy:?}");
            let allowed_names = debug
                .split("allowed_names: ")
                .nth(1)
                .unwrap()
                .split(", missing_allowed")
                .next()
                .unwrap();
            assert!(allowed_names.contains("HOME"));
            assert!(!allowed_names.contains("OPENAI_API_KEY"));
            assert!(!allowed_names.contains("OPENROUTER_API_KEY"));
            assert!(!allowed_names.contains("PRIME_AGENT_CODING_AGENT_DIR"));
            assert!(!allowed_names.contains("PI_CODING_AGENT_DIR"));
            assert!(!allowed_names.contains(PRIME_AGENT_TELEMETRY_CONTROL.0));
            let protected = policy.output_protected_strings();
            assert!(protected.iter().any(|value| value == "/fixture/home"));
            assert!(!protected.iter().any(|value| value == raw_secret));
            assert!(!protected
                .iter()
                .any(|value| value.contains("store-override")));
        }
    }

    #[test]
    fn every_installed_version_probe_environment_is_provider_credential_free() {
        let raw_secret = "version-probe-secret-must-not-appear";
        let ambient = [
            ("PATH", "/fixture/bin"),
            ("HOME", "/fixture/home"),
            ("USERPROFILE", "/fixture/profile"),
            ("XDG_CONFIG_HOME", "/fixture/xdg/config"),
            ("XDG_CACHE_HOME", "/fixture/xdg/cache"),
            ("XDG_DATA_HOME", "/fixture/xdg/data"),
            ("SSH_AUTH_SOCK", "/fixture/ssh-agent.sock"),
            ("OPENAI_API_KEY", raw_secret),
            ("CODEX_HOME", raw_secret),
            ("CODEX_SQLITE_HOME", raw_secret),
            ("CODEX_ACCESS_TOKEN", raw_secret),
            ("ANTHROPIC_API_KEY", raw_secret),
            ("ANTHROPIC_OAUTH_TOKEN", raw_secret),
            ("OPENROUTER_API_KEY", raw_secret),
            ("GEMINI_API_KEY", raw_secret),
            ("GOOGLE_APPLICATION_CREDENTIALS", raw_secret),
            ("GITHUB_TOKEN", raw_secret),
            ("GH_TOKEN", raw_secret),
            ("COPILOT_GITHUB_TOKEN", raw_secret),
            ("AWS_ACCESS_KEY_ID", raw_secret),
            ("AWS_SECRET_ACCESS_KEY", raw_secret),
            ("AWS_SESSION_TOKEN", raw_secret),
        ];
        for adapter in [
            InstalledAdapter::CodexExecJson,
            InstalledAdapter::ClaudePrintStreamJson,
            InstalledAdapter::PrimeRpc,
            InstalledAdapter::OmpRpc,
        ] {
            let policy = version_probe_environment(
                adapter,
                ambient
                    .iter()
                    .map(|(name, value)| (OsString::from(name), OsString::from(value))),
            );
            let debug = format!("{policy:?}");
            let allowed_names = debug
                .split("allowed_names: ")
                .nth(1)
                .unwrap()
                .split(", missing_allowed")
                .next()
                .unwrap();
            assert!(allowed_names.contains("PATH"), "{}", adapter.id());
            for forbidden in [
                "HOME",
                "USERPROFILE",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_DATA_HOME",
                "SSH_AUTH_SOCK",
                "OPENAI_API_KEY",
                "CODEX_HOME",
                "CODEX_SQLITE_HOME",
                "CODEX_ACCESS_TOKEN",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_OAUTH_TOKEN",
                "OPENROUTER_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "COPILOT_GITHUB_TOKEN",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
            ] {
                assert!(
                    !allowed_names.contains(forbidden),
                    "{} leaked {forbidden}",
                    adapter.id()
                );
            }
            assert!(
                !policy
                    .output_protected_strings()
                    .iter()
                    .any(|value| value == raw_secret),
                "{} retained a provider secret",
                adapter.id()
            );
        }
    }

    #[test]
    fn prime_and_omp_environment_routes_use_fresh_guard_owned_private_config_directories() {
        for (adapter, config_name, ambient_override) in [
            (
                InstalledAdapter::PrimeRpc,
                "PRIME_AGENT_CODING_AGENT_DIR",
                "PI_CODING_AGENT_DIR",
            ),
            (
                InstalledAdapter::OmpRpc,
                "PI_CODING_AGENT_DIR",
                "PRIME_AGENT_CODING_AGENT_DIR",
            ),
        ] {
            let make = || {
                let environment = environment_policy(
                    adapter,
                    "openrouter",
                    [
                        (OsString::from("PATH"), OsString::from("/bin")),
                        (
                            OsString::from("OPENROUTER_API_KEY"),
                            OsString::from("fixture-key"),
                        ),
                        (
                            OsString::from(config_name),
                            OsString::from("/fixture/ambient-store-override"),
                        ),
                        (
                            OsString::from(ambient_override),
                            OsString::from("/fixture/other-ambient-store-override"),
                        ),
                        (OsString::from("PRIME_AGENT_TELEMETRY"), OsString::from("1")),
                    ],
                )
                .unwrap();
                prepare_launch(
                    adapter,
                    PathBuf::from("/fixture/adapter"),
                    Path::new("/fixture"),
                    b"image",
                    FRAMING,
                    TASK.as_bytes(),
                    "fixture",
                    Some("fixture/model"),
                    "openrouter",
                    environment,
                )
                .unwrap()
            };
            let first = make();
            let second = make();
            let first_path = first.credential_config_directory().unwrap().to_owned();
            let second_path = second.credential_config_directory().unwrap().to_owned();
            assert_ne!(first_path, second_path);
            assert_eq!(
                first_path.file_name(),
                Some(OsStr::new("credential-config"))
            );
            assert!(first_path.is_dir());
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt as _;
                assert_eq!(
                    fs::metadata(&first_path).unwrap().permissions().mode() & 0o777,
                    0o700
                );
            }
            let debug = format!("{:?}", first.environment);
            let allowed_names = debug
                .split("allowed_names: ")
                .nth(1)
                .unwrap()
                .split("],")
                .next()
                .unwrap();
            assert!(allowed_names.contains(config_name));
            assert!(!allowed_names.contains(ambient_override));
            assert_eq!(
                allowed_names.contains(PRIME_AGENT_TELEMETRY_CONTROL.0),
                adapter == InstalledAdapter::PrimeRpc
            );
            if adapter == InstalledAdapter::PrimeRpc {
                let protected = first.environment.output_protected_strings();
                assert!(!protected.iter().any(|value| value == "0"));
                assert!(!protected.iter().any(|value| value == "1"));
            }
            assert!(!debug.contains("ambient-store-override"));
            drop(first);
            drop(second);
            assert!(!first_path.exists());
            assert!(!second_path.exists());

            let login_group = match adapter {
                InstalledAdapter::PrimeRpc => "prime-harness-login",
                InstalledAdapter::OmpRpc => "omp-harness-login",
                _ => unreachable!(),
            };
            for auth_group in adapter
                .auth_profiles()
                .iter()
                .copied()
                .filter(|group| *group != login_group)
            {
                let environment = environment_policy(
                    adapter,
                    auth_group,
                    [
                        ("ANTHROPIC_API_KEY", "fixture"),
                        ("ANTHROPIC_OAUTH_TOKEN", "fixture"),
                        ("OPENAI_API_KEY", "fixture"),
                        ("OPENROUTER_API_KEY", "fixture"),
                        ("GEMINI_API_KEY", "fixture"),
                        ("GOOGLE_APPLICATION_CREDENTIALS", "/fixture/google.json"),
                        ("GOOGLE_CLOUD_PROJECT", "fixture"),
                        ("GOOGLE_CLOUD_LOCATION", "fixture"),
                        ("GITHUB_TOKEN", "fixture"),
                        ("GH_TOKEN", "fixture"),
                        ("COPILOT_GITHUB_TOKEN", "fixture"),
                        ("AWS_ACCESS_KEY_ID", "fixture"),
                        ("AWS_SECRET_ACCESS_KEY", "fixture"),
                        ("AWS_SESSION_TOKEN", "fixture"),
                        ("AWS_PROFILE", "fixture"),
                        ("AWS_REGION", "fixture"),
                        ("AWS_DEFAULT_REGION", "fixture"),
                        (config_name, "/fixture/ambient-store-override"),
                        (ambient_override, "/fixture/other-ambient-store-override"),
                        ("PRIME_AGENT_TELEMETRY", "1"),
                    ]
                    .into_iter()
                    .map(|(name, value)| (OsString::from(name), OsString::from(value))),
                )
                .unwrap();
                let launch = prepare_launch(
                    adapter,
                    PathBuf::from("/fixture/adapter"),
                    Path::new("/fixture"),
                    b"image",
                    FRAMING,
                    TASK.as_bytes(),
                    "fixture",
                    Some("fixture/model"),
                    auth_group,
                    environment,
                )
                .unwrap();
                assert!(
                    launch.credential_config_directory().is_some(),
                    "{}/{} did not receive private config isolation",
                    adapter.id(),
                    auth_group
                );
            }
            let login = prepare_launch(
                adapter,
                PathBuf::from("/fixture/adapter"),
                Path::new("/fixture"),
                b"image",
                FRAMING,
                TASK.as_bytes(),
                "fixture",
                Some("fixture/model"),
                login_group,
                environment_policy(
                    adapter,
                    login_group,
                    [(OsString::from("PRIME_AGENT_TELEMETRY"), OsString::from("1"))],
                )
                .unwrap(),
            )
            .unwrap();
            assert!(login.credential_config_directory().is_none());
            let login_debug = format!("{:?}", login.environment);
            let login_allowed_names = login_debug
                .split("allowed_names: ")
                .nth(1)
                .unwrap()
                .split(", missing_allowed")
                .next()
                .unwrap();
            assert!(!login_allowed_names.contains(config_name));
            assert_eq!(
                login_allowed_names.contains(PRIME_AGENT_TELEMETRY_CONTROL.0),
                adapter == InstalledAdapter::PrimeRpc
            );
            if adapter == InstalledAdapter::PrimeRpc {
                let protected = login.environment.output_protected_strings();
                assert!(!protected.iter().any(|value| value == "0"));
                assert!(!protected.iter().any(|value| value == "1"));
            }
        }
    }

    #[test]
    fn oversized_claude_task_prime_total_and_windows_utf16_fail_before_spawn() {
        let oversized_task = format!(r#"{{"argv":["{}"]}}"#, "x".repeat(140_000));
        assert_eq!(
            prepare_launch(
                InstalledAdapter::ClaudePrintStreamJson,
                PathBuf::from("/fixture/claude"),
                Path::new("/fixture"),
                b"image",
                FRAMING,
                oversized_task.as_bytes(),
                "fixture",
                None,
                "claude-subscription",
                empty_environment(InstalledAdapter::ClaudePrintStreamJson),
            )
            .unwrap_err()
            .code,
            ErrorCode::ConfigInvalid
        );
        let argv = vec![
            "--cwd".into(),
            "c".repeat(20_000).into(),
            "--append-system-prompt".into(),
            "i".repeat(125_000).into(),
            "--model".into(),
            "m".repeat(125_000).into(),
        ];
        assert_eq!(
            assert_argv_limits(
                InstalledAdapter::PrimeRpc,
                Path::new("/fixture/prime-agent"),
                &argv,
                HostPlatform {
                    os: "linux",
                    arch: "x86_64",
                    libc: Some("gnu")
                },
            )
            .unwrap_err()
            .code,
            ErrorCode::ConfigInvalid
        );
        assert_eq!(
            assert_argv_limits(
                InstalledAdapter::PrimeRpc,
                Path::new("C:\\fixture\\prime-agent.exe"),
                &["😀".repeat(4_096).into()],
                HostPlatform {
                    os: "windows",
                    arch: "x86_64",
                    libc: None
                },
            )
            .unwrap_err()
            .code,
            ErrorCode::ConfigInvalid
        );
    }

    #[test]
    fn recipe_drives_rpc_stdin_retention_until_the_terminal_event() {
        assert_eq!(
            InstalledAdapter::PrimeRpc.recipe_stdin_lifecycle(),
            StdinLifecycle::CloseAfterTerminalEvent
        );
        assert_eq!(
            InstalledAdapter::OmpRpc.recipe_stdin_lifecycle(),
            StdinLifecycle::CloseAfterTerminalEvent
        );
        for adapter in [
            InstalledAdapter::CodexExecJson,
            InstalledAdapter::ClaudePrintStreamJson,
        ] {
            assert_eq!(
                adapter.recipe_stdin_lifecycle(),
                StdinLifecycle::CloseAfterWrite
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn executable_resolution_requires_an_executable_file_and_preserves_path_order() {
        use std::os::unix::fs::PermissionsExt as _;

        let root = TempDir::new().unwrap();
        let first = root.path().join("first");
        let second = root.path().join("second");
        fs::create_dir_all(&first).unwrap();
        fs::create_dir_all(&second).unwrap();
        let not_executable = first.join("codex");
        fs::write(&not_executable, b"fixture").unwrap();
        fs::set_permissions(&not_executable, fs::Permissions::from_mode(0o600)).unwrap();
        let executable = second.join("codex");
        fs::write(&executable, b"fixture").unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let search = std::env::join_paths([&first, &second]).unwrap();
        assert_eq!(
            resolve_executable(InstalledAdapter::CodexExecJson, Some(&search)),
            Some(executable.canonicalize().unwrap())
        );
    }

    #[test]
    fn invalid_auth_group_and_nul_or_oversized_inputs_fail_before_spawn() {
        assert_eq!(
            environment_policy(InstalledAdapter::CodexExecJson, "ambiguous", [])
                .unwrap_err()
                .code,
            ErrorCode::ConfigInvalid
        );
        let environment = empty_environment(InstalledAdapter::PrimeRpc);
        assert_eq!(
            prepare_launch(
                InstalledAdapter::PrimeRpc,
                PathBuf::from("/fixture/prime-agent"),
                Path::new("/fixture"),
                b"image\0",
                FRAMING,
                TASK.as_bytes(),
                "fixture",
                None,
                "prime-harness-login",
                environment,
            )
            .unwrap_err()
            .code,
            ErrorCode::ImageInvalid
        );
        let environment = empty_environment(InstalledAdapter::PrimeRpc);
        assert_eq!(
            prepare_launch(
                InstalledAdapter::PrimeRpc,
                PathBuf::from("/fixture/prime-agent"),
                Path::new("/fixture"),
                &vec![b'x'; MAX_PRIME_INLINE_IMAGE_BYTES + 1],
                FRAMING,
                TASK.as_bytes(),
                "fixture",
                None,
                "prime-harness-login",
                environment,
            )
            .unwrap_err()
            .code,
            ErrorCode::ImageTooLarge
        );

        for adapter in [
            InstalledAdapter::CodexExecJson,
            InstalledAdapter::ClaudePrintStreamJson,
            InstalledAdapter::OmpRpc,
        ] {
            assert!(
                prepare_launch(
                    adapter,
                    PathBuf::from("/fixture/adapter"),
                    Path::new("/fixture"),
                    &vec![b'x'; MAX_PRIME_INLINE_IMAGE_BYTES + 1],
                    FRAMING,
                    TASK.as_bytes(),
                    "fixture",
                    None,
                    adapter.default_probe_auth_group(),
                    empty_environment(adapter),
                )
                .is_ok(),
                "{} should not inherit Prime's inline argv cap",
                adapter.id()
            );
        }
    }

    #[test]
    fn readiness_probe_arguments_and_classification_are_harness_specific() {
        assert!(InstalledAdapter::PrimeRpc.auth_probe().is_none());
        assert!(InstalledAdapter::OmpRpc.auth_probe().is_none());
        assert_eq!(
            InstalledAdapter::CodexExecJson.auth_probe().unwrap().argv,
            ["login", "status"]
        );
        assert_eq!(
            InstalledAdapter::ClaudePrintStreamJson
                .auth_probe()
                .unwrap()
                .argv,
            ["auth", "status", "--json"]
        );
        assert_eq!(
            InstalledAdapter::CodexExecJson
                .classify_auth_probe(&CommandProbeOutcome {
                    exit_code: 0,
                    stdout: "Logged in using ChatGPT".to_owned(),
                    stderr: String::new(),
                })
                .unwrap(),
            "ready"
        );
        assert_eq!(
            InstalledAdapter::ClaudePrintStreamJson
                .classify_auth_probe(&CommandProbeOutcome {
                    exit_code: 0,
                    stdout: r#"{"loggedIn":true}"#.to_owned(),
                    stderr: String::new(),
                })
                .unwrap(),
            "ready"
        );
        assert_eq!(
            InstalledAdapter::CodexExecJson
                .classify_auth_probe(&CommandProbeOutcome {
                    exit_code: 0,
                    stdout: "Not logged in".to_owned(),
                    stderr: String::new(),
                })
                .unwrap_err()
                .code,
            ErrorCode::HarnessNeedsAuth
        );
    }

    #[test]
    fn frozen_protocol_records_normalize_only_transport_terminal_facts() {
        let scenarios = [
            (
                InstalledAdapter::CodexExecJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/codex-exec-json.v1.json"
                ),
            ),
            (
                InstalledAdapter::ClaudePrintStreamJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/claude-print-stream-json.v1.json"
                ),
            ),
            (
                InstalledAdapter::PrimeRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"),
            ),
            (
                InstalledAdapter::OmpRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/omp-rpc.v1.json"),
            ),
        ];
        for (adapter, scenario) in scenarios {
            let scenario: Value = serde_json::from_str(scenario).unwrap();
            let records = scenario["fakeStdout"].as_array().unwrap();
            let normalized =
                normalize_transport(adapter, records, "fixture-invocation-0001").unwrap();
            assert_eq!(normalized.assistant_messages.len(), 1);
            assert!(normalized.assistant_messages[0].starts_with("Echoed task argv:"));
            let task: Value = serde_json::from_str(TASK).unwrap();
            let argv = task["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|value| value.as_str().unwrap().to_owned())
                .collect::<Vec<_>>();
            let terminal =
                recover_terminal(&echo_image(), &normalized.assistant_messages, &argv).unwrap();
            assert_eq!(terminal.envelope["semanticStatus"], "not-applicable");
            assert!(!normalized.terminal_event.is_empty());
        }
    }

    #[test]
    fn incremental_assistant_projection_validates_each_adapter_prefix() {
        let scenarios = [
            (
                InstalledAdapter::CodexExecJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/codex-exec-json.v1.json"
                ),
            ),
            (
                InstalledAdapter::ClaudePrintStreamJson,
                include_str!(
                    "../../../../shared/fixtures/adapters/scenarios/claude-print-stream-json.v1.json"
                ),
            ),
            (
                InstalledAdapter::PrimeRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"),
            ),
            (
                InstalledAdapter::OmpRpc,
                include_str!("../../../../shared/fixtures/adapters/scenarios/omp-rpc.v1.json"),
            ),
        ];
        for (adapter, scenario) in scenarios {
            let scenario: Value = serde_json::from_str(scenario).unwrap();
            let records = scenario["fakeStdout"].as_array().unwrap();
            let candidate_index = records
                .iter()
                .position(|record| match adapter {
                    InstalledAdapter::AgentsSdkJsonl => unreachable!("historical scenario suite"),
                    InstalledAdapter::CodexExecJson => {
                        record_type(record) == Some("item.completed")
                            && record.pointer("/item/type").and_then(Value::as_str)
                                == Some("agent_message")
                    }
                    InstalledAdapter::ClaudePrintStreamJson => {
                        record_type(record) == Some("assistant")
                    }
                    InstalledAdapter::PrimeRpc | InstalledAdapter::OmpRpc => {
                        record_type(record) == Some("message_end")
                            && record.pointer("/message/role").and_then(Value::as_str)
                                == Some("assistant")
                    }
                })
                .unwrap();
            let projected = admitted_assistant_messages(
                adapter,
                &records[..=candidate_index],
                "fixture-invocation-0001",
            )
            .unwrap();
            assert_eq!(
                projected,
                normalize_transport(adapter, records, "fixture-invocation-0001")
                    .unwrap()
                    .assistant_messages,
                "{}",
                adapter.id()
            );

            let mut invalid_prefix = records[..=candidate_index].to_vec();
            match adapter {
                InstalledAdapter::AgentsSdkJsonl => unreachable!("historical scenario suite"),
                InstalledAdapter::CodexExecJson => {
                    invalid_prefix[candidate_index]["thread_id"] = json!("wrong-thread");
                }
                InstalledAdapter::ClaudePrintStreamJson => {
                    invalid_prefix[candidate_index]["session_id"] = json!("wrong-session");
                }
                InstalledAdapter::PrimeRpc => {
                    invalid_prefix[0]["id"] = json!("wrong-response");
                }
                InstalledAdapter::OmpRpc => {
                    let commands = invalid_prefix
                        .iter()
                        .position(|record| record_type(record) == Some("available_commands_update"))
                        .unwrap();
                    invalid_prefix.remove(commands);
                }
            }
            assert_eq!(
                admitted_assistant_messages(adapter, &invalid_prefix, "fixture-invocation-0001")
                    .unwrap_err()
                    .code,
                ErrorCode::ProtocolMalformed,
                "{}",
                adapter.id()
            );
        }
    }

    #[test]
    fn terminal_recovery_requires_one_exact_minified_final_line() {
        let image = echo_image();
        let expected = vec![
            "prose".to_owned(),
            "run".to_owned(),
            "hello.prose.md".to_owned(),
        ];
        let terminal = format!(
            "{{\"schema\":\"openprose.echo-terminal/1\",\"semanticStatus\":\"not-applicable\",\"placeholder\":true,\"marker\":\"OPENPROSE_ECHO_TERMINAL_V0\",\"task\":{{\"argv\":{}}}}}",
            serde_json::to_string(&expected).unwrap()
        );
        let recovered = recover_terminal(
            &image,
            &[format!("Echoed task argv: hello\n{terminal}\n\n")],
            &expected,
        )
        .unwrap();
        assert_eq!(recovered.visible_text, "Echoed task argv: hello");
        assert_eq!(recovered.envelope["task"]["argv"], json!(expected));

        let pretty =
            serde_json::to_string_pretty(&serde_json::from_str::<Value>(&terminal).unwrap())
                .unwrap();
        let duplicate_schema =
            terminal.replacen('{', "{\"schema\":\"openprose.echo-terminal/1\",", 1);
        let alternate_escape = terminal.replacen(
            "openprose.echo-terminal/1",
            "openprose.echo-terminal\\/1",
            1,
        );
        for invalid in [
            "Echoed task argv: hello".to_owned(),
            "{\"schema\":\"openprose.echo-terminal/1\"}".to_owned(),
            "{\"schema\":\"openprose.echo-terminal/1\",\"semanticStatus\":\"not-applicable\",\"placeholder\":true,\"marker\":\"OPENPROSE_ECHO_TERMINAL_V0\",\"task\":{\"argv\":[\"wrong\"]}}".to_owned(),
            format!(" {terminal}"),
            format!("{terminal} "),
            format!("terminal: {terminal}"),
            format!("{terminal} commentary"),
            terminal.replacen("\":\"", "\": \"", 1),
            format!("```json\n{terminal}\n```"),
            format!("{terminal}\r"),
            pretty,
            duplicate_schema,
            alternate_escape,
        ] {
            assert_eq!(
                recover_terminal(&image, &[invalid], &expected).unwrap_err().code,
                ErrorCode::ProtocolMalformed
            );
        }
    }

    #[test]
    fn terminal_recovery_binds_task_only_when_the_closed_image_schema_requires_it() {
        let expected = vec![
            "prose".to_owned(),
            "run".to_owned(),
            "hello.prose.md".to_owned(),
        ];
        let sentinel = r#"{"schema":"openprose.sentinel-terminal-envelope/1","semanticStatus":"not-applicable","marker":"OPENPROSE_SENTINEL_TERMINAL_V1"}"#;
        let recovered = recover_terminal(
            &sentinel_image(),
            &[format!("transport complete\n{sentinel}")],
            &expected,
        )
        .unwrap();
        assert_eq!(
            recovered.envelope["marker"],
            "OPENPROSE_SENTINEL_TERMINAL_V1"
        );
        assert_eq!(recovered.visible_text, "transport complete");

        let undeclared_task = sentinel.replacen('}', r#","task":{"argv":["prose"]}}"#, 1);
        assert_eq!(
            recover_terminal(&sentinel_image(), &[undeclared_task], &expected)
                .unwrap_err()
                .code,
            ErrorCode::ProtocolMalformed
        );

        let mut optional_task_image = sentinel_image();
        let mut optional_task_schema: Value =
            serde_json::from_slice(&optional_task_image.terminal_envelope_schema).unwrap();
        optional_task_schema["properties"]["task"] = json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["argv"],
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}}
            }
        });
        optional_task_image.terminal_envelope_schema =
            serde_json::to_vec(&optional_task_schema).unwrap();
        assert!(
            recover_terminal(&optional_task_image, &[sentinel.to_owned()], &expected).is_ok(),
            "an optional schema property must not become runner-required"
        );
        let optional_task = sentinel.replacen('}', r#","task":{"argv":["different"]}}"#, 1);
        assert!(
            recover_terminal(&optional_task_image, &[optional_task], &expected).is_ok(),
            "an optional schema property must remain governed by the image schema"
        );
    }

    #[test]
    fn echo_terminal_still_requires_and_exactly_binds_task_argv() {
        let expected = vec!["prose".to_owned(), "write".to_owned(), "hello".to_owned()];
        let absent = r#"{"schema":"openprose.echo-terminal/1","semanticStatus":"not-applicable","placeholder":true,"marker":"OPENPROSE_ECHO_TERMINAL_V0"}"#;
        let wrong = r#"{"schema":"openprose.echo-terminal/1","semanticStatus":"not-applicable","placeholder":true,"marker":"OPENPROSE_ECHO_TERMINAL_V0","task":{"argv":["wrong"]}}"#;
        for terminal in [absent, wrong] {
            assert_eq!(
                recover_terminal(&echo_image(), &[terminal.to_owned()], &expected)
                    .unwrap_err()
                    .code,
                ErrorCode::ProtocolMalformed
            );
        }
    }

    #[test]
    fn rpc_response_correlation_and_terminal_shape_fail_closed() {
        let scenario: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"
        ))
        .unwrap();
        let records = scenario["fakeStdout"].as_array().unwrap();
        let normalized = normalize_transport(
            InstalledAdapter::PrimeRpc,
            records,
            "fixture-invocation-0001",
        )
        .unwrap();
        assert_eq!(normalized.terminal_event, "agent_end");
        assert_eq!(normalized.assistant_messages.len(), 1);

        assert!(normalized.assistant_messages[0].contains("OPENPROSE_ECHO_TERMINAL_V0"));
        let mut atomic_thinking = records.clone();
        atomic_thinking.drain(7..10);
        assert!(
            normalize_transport(
                InstalledAdapter::PrimeRpc,
                &atomic_thinking,
                "fixture-invocation-0001"
            )
            .is_ok(),
            "Prime may settle thinking atomically when it emits no thinking_delta records"
        );
        let mut exact_cumulative_thinking = records.clone();
        for pointer in [
            "/assistantMessageEvent/delta",
            "/message/content/0/thinking",
        ] {
            let value = exact_cumulative_thinking[9]
                .pointer_mut(pointer)
                .unwrap()
                .as_str()
                .unwrap()
                .strip_suffix("\n\n")
                .unwrap()
                .to_owned();
            *exact_cumulative_thinking[9].pointer_mut(pointer).unwrap() = json!(value);
        }
        assert!(
            normalize_transport(
                InstalledAdapter::PrimeRpc,
                &exact_cumulative_thinking,
                "fixture-invocation-0001"
            )
            .is_ok(),
            "Prime may settle thinking with exact cumulative equality"
        );
        assert_eq!(
            normalize_transport(InstalledAdapter::PrimeRpc, records, "wrong-id")
                .unwrap_err()
                .code,
            ErrorCode::ProtocolMalformed
        );
        let truncated = &records[..records.len() - 1];
        assert_eq!(
            normalize_transport(
                InstalledAdapter::PrimeRpc,
                truncated,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );
    }

    #[test]
    fn prime_text_only_index_zero_is_admitted_but_closed_adjacent_branches_are_not() {
        let scenario: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"
        ))
        .unwrap();
        let records = scenario["fakeStdout"].as_array().unwrap();
        let text_only = prime_text_only_records(records);
        let normalized = normalize_transport(
            InstalledAdapter::PrimeRpc,
            &text_only,
            "fixture-invocation-0001",
        )
        .unwrap();
        assert_eq!(normalized.terminal_event, "agent_end");
        assert_eq!(normalized.assistant_messages.len(), 1);

        let mut atomic_text = text_only.clone();
        atomic_text.drain(7..10);
        assert!(normalize_transport(
            InstalledAdapter::PrimeRpc,
            &atomic_text,
            "fixture-invocation-0001",
        )
        .is_ok());

        let mut empty_deltas_then_atomic_text = text_only.clone();
        for record in &mut empty_deltas_then_atomic_text[6..] {
            if record
                .pointer("/assistantMessageEvent/type")
                .and_then(Value::as_str)
                == Some("text_delta")
            {
                record["assistantMessageEvent"]["delta"] = json!("");
                record["message"]["content"][0]["text"] = json!("");
            }
        }
        assert!(normalize_transport(
            InstalledAdapter::PrimeRpc,
            &empty_deltas_then_atomic_text,
            "fixture-invocation-0001",
        )
        .is_ok());

        let mut invalid_first_content = text_only.clone();
        invalid_first_content[6]["assistantMessageEvent"]["contentIndex"] = json!(1);
        let first_content_error = normalize_transport(
            InstalledAdapter::PrimeRpc,
            &invalid_first_content,
            "fixture-invocation-0001",
        )
        .unwrap_err();
        assert_eq!(
            first_content_error.details.unwrap()["adapterDiagnostic"]["phase"],
            "await-thinking-or-text-start",
        );

        let mut thinking_only = records.clone();
        thinking_only.drain(11..16);
        let thinking_message = thinking_only[10]["message"].clone();
        thinking_only[11]["message"] = thinking_message.clone();
        thinking_only[12]["message"] = thinking_message.clone();
        thinking_only[13]["messages"][1] = thinking_message;

        let mut empty_text = text_only.clone();
        empty_text.drain(7..10);
        for record in &mut empty_text[6..] {
            if record
                .pointer("/assistantMessageEvent/type")
                .and_then(Value::as_str)
                == Some("text_end")
            {
                record["assistantMessageEvent"]["content"] = json!("");
            }
            if let Some(message) = record.get_mut("message") {
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    message["content"][0]["text"] = json!("");
                }
            }
            if let Some(message) = record.pointer_mut("/messages/1") {
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    message["content"][0]["text"] = json!("");
                }
            }
        }

        let mut empty_delta_text = text_only.clone();
        for record in &mut empty_delta_text[6..] {
            if record
                .pointer("/assistantMessageEvent/type")
                .and_then(Value::as_str)
                == Some("text_delta")
            {
                record["assistantMessageEvent"]["delta"] = json!("");
            }
            if record
                .pointer("/assistantMessageEvent/type")
                .and_then(Value::as_str)
                == Some("text_end")
            {
                record["assistantMessageEvent"]["content"] = json!("");
            }
            if let Some(message) = record.get_mut("message") {
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    message["content"][0]["text"] = json!("");
                }
            }
            if let Some(message) = record.pointer_mut("/messages/1") {
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    message["content"][0]["text"] = json!("");
                }
            }
        }

        let mut tool = text_only.clone();
        let mut tool_message = tool[5]["message"].clone();
        tool_message["content"] =
            json!([{"type":"toolCall","id":"call-1","name":"tool","arguments":{}}]);
        tool.insert(
            6,
            json!({
                "type":"message_update",
                "assistantMessageEvent":{"type":"toolcall_start","contentIndex":0},
                "message":tool_message,
            }),
        );

        let mut multiple_blocks = text_only.clone();
        multiple_blocks[6]["message"]["content"]
            .as_array_mut()
            .unwrap()
            .push(json!({"type":"text","text":""}));

        let mut post_text_thinking = text_only.clone();
        let final_text = post_text_thinking[10]["message"]["content"][0].clone();
        let mut post_text_message = post_text_thinking[10]["message"].clone();
        post_text_message["content"] = json!([final_text,{"type":"thinking","thinking":""}]);
        post_text_thinking.insert(
            11,
            json!({
                "type":"message_update",
                "assistantMessageEvent":{"type":"thinking_start","contentIndex":1},
                "message":post_text_message,
            }),
        );

        for (name, rejected) in [
            ("thinking-only", thinking_only),
            ("empty-text", empty_text),
            ("empty-delta-text", empty_delta_text),
            ("tool", tool),
            ("multiple-blocks", multiple_blocks),
            ("post-text-thinking", post_text_thinking),
        ] {
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::PrimeRpc,
                    &rejected,
                    "fixture-invocation-0001",
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed,
                "{name} must remain outside the closed functional-alpha lifecycle",
            );
        }
    }

    #[test]
    fn prime_atomic_redacted_thinking_then_text_remains_admitted() {
        let scenario: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"
        ))
        .unwrap();
        let mut records = scenario["fakeStdout"].as_array().unwrap().clone();
        records.drain(7..10);
        for index in [6, 7] {
            records[index]["message"]["content"][0]["thinkingSignature"] =
                json!("opaque-redacted-reasoning");
            records[index]["message"]["content"][0]["redacted"] = json!(true);
        }
        records[7]["assistantMessageEvent"]["content"] = json!("[Reasoning redacted]");
        records[7]["message"]["content"][0]["thinking"] = json!("[Reasoning redacted]");
        for record in &mut records[8..] {
            if let Some(message) = record.get_mut("message") {
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    message["content"][0]["thinking"] = json!("[Reasoning redacted]");
                    message["content"][0]["thinkingSignature"] = json!("opaque-redacted-reasoning");
                    message["content"][0]["redacted"] = json!(true);
                }
            }
            if let Some(message) = record.pointer_mut("/messages/1") {
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    message["content"][0]["thinking"] = json!("[Reasoning redacted]");
                    message["content"][0]["thinkingSignature"] = json!("opaque-redacted-reasoning");
                    message["content"][0]["redacted"] = json!(true);
                }
            }
        }
        assert!(normalize_transport(
            InstalledAdapter::PrimeRpc,
            &records,
            "fixture-invocation-0001",
        )
        .is_ok());
    }

    #[test]
    fn prime_no_tool_lifecycle_rejects_missing_duplicate_unknown_and_post_terminal_records() {
        let scenario: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"
        ))
        .unwrap();
        let records = scenario["fakeStdout"].as_array().unwrap();
        for omitted in 0..records.len() {
            let mut mutated = records.clone();
            mutated.remove(omitted);
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::PrimeRpc,
                    &mutated,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed,
                "omitting frame {omitted} must fail closed"
            );
        }

        let mut duplicate = records.clone();
        duplicate.insert(2, records[1].clone());
        let mut reordered = records.clone();
        reordered.swap(3, 5);
        let mut unknown = records.clone();
        unknown.insert(6, json!({"type":"notice"}));
        let mut post_terminal = records.clone();
        post_terminal.push(json!({"type":"notice"}));
        for mutated in [duplicate, reordered, unknown, post_terminal] {
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::PrimeRpc,
                    &mutated,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }
    }

    #[test]
    fn prime_no_tool_lifecycle_binds_roles_updates_settlement_and_bounds() {
        let scenario: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json"
        ))
        .unwrap();
        let records = scenario["fakeStdout"].as_array().unwrap();
        let mutations: &[(usize, &str, Value)] = &[
            (0, "/id", json!("wrong")),
            (3, "/message/role", json!("assistant")),
            (4, "/message/content", json!("different")),
            (6, "/assistantMessageEvent/type", json!("thinking_delta")),
            (7, "/assistantMessageEvent/contentIndex", json!(1)),
            (8, "/assistantMessageEvent/type", json!("text_delta")),
            (9, "/assistantMessageEvent/delta", json!("different")),
            (9, "/message/content/0/thinking", json!("different")),
            (10, "/assistantMessageEvent/content", json!("different")),
            (11, "/assistantMessageEvent/type", json!("text_delta")),
            (12, "/assistantMessageEvent/delta", json!("different")),
            (13, "/message/content/1/text", json!("different")),
            (15, "/assistantMessageEvent/content", json!("different")),
            (16, "/message/content/1/text", json!("different")),
            (17, "/toolResults", json!([{"role":"toolResult"}])),
            (18, "/messages", json!([])),
        ];
        for (index, pointer, replacement) in mutations {
            let mut mutated = records.clone();
            *mutated[*index].pointer_mut(pointer).unwrap() = replacement.clone();
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::PrimeRpc,
                    &mutated,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed,
                "mutation at frame {index} pointer {pointer} must fail closed"
            );
        }

        for suffix in ["\n", "\n\n\n", "  ", "\n "] {
            let mut mutated = records.clone();
            for pointer in [
                "/assistantMessageEvent/delta",
                "/message/content/0/thinking",
            ] {
                let value = mutated[9]
                    .pointer_mut(pointer)
                    .unwrap()
                    .as_str()
                    .unwrap()
                    .strip_suffix("\n\n")
                    .unwrap()
                    .to_owned()
                    + suffix;
                *mutated[9].pointer_mut(pointer).unwrap() = json!(value);
            }
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::PrimeRpc,
                    &mutated,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed,
                "thinking settlement must reject suffix {suffix:?}"
            );
        }
        let mut settled_drift = records.clone();
        settled_drift[10]["assistantMessageEvent"]["content"] =
            json!("synthetic reasoning summary drift");
        settled_drift[10]["message"]["content"][0]["thinking"] =
            json!("synthetic reasoning summary drift");
        assert_eq!(
            normalize_transport(
                InstalledAdapter::PrimeRpc,
                &settled_drift,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        for invalid_content in [
            json!("legacy string"),
            json!([]),
            json!([{"type":"text","text":""}]),
            json!([{"type":"text","text":"x","extra":true}]),
            json!([{"type":"image","text":"x"}]),
        ] {
            let mut mutated = records.clone();
            mutated[3]["message"]["content"] = invalid_content;
            let error = normalize_transport(
                InstalledAdapter::PrimeRpc,
                &mutated,
                "fixture-invocation-0001",
            )
            .unwrap_err();
            assert_eq!(error.code, ErrorCode::ProtocolMalformed);
            assert_eq!(
                error.details.unwrap()["adapterDiagnostic"],
                json!({
                    "schema":"openprose.adapter-diagnostic/1",
                    "adapterId":"prime/rpc",
                    "stage":"prime-lifecycle",
                    "phase":"await-user-message-start",
                    "counters":{
                        "acceptedRecords":3,
                        "thinkingDeltas":0,
                        "textDeltas":0,
                        "saturated":false,
                    }
                })
            );
        }

        let secret = "provider-secret-must-not-leak";
        let mut malformed_user = records.clone();
        malformed_user[3]["message"]["content"] =
            json!([{"type":"text","text":"","signature":secret}]);
        let error = normalize_transport(
            InstalledAdapter::PrimeRpc,
            &malformed_user,
            "fixture-invocation-0001",
        )
        .unwrap_err();
        assert!(!serde_json::to_string(&error).unwrap().contains(secret));

        for (index, pointer, phase, accepted, thinking, text) in [
            (
                9,
                "/assistantMessageEvent/delta",
                "await-thinking-delta-or-end",
                9,
                2,
                0,
            ),
            (
                13,
                "/assistantMessageEvent/delta",
                "await-text-delta-or-end",
                13,
                3,
                1,
            ),
        ] {
            let mut mutated = records.clone();
            let canary = format!("candidate-secret-{index}");
            *mutated[index].pointer_mut(pointer).unwrap() = json!(canary);
            let error = normalize_transport(
                InstalledAdapter::PrimeRpc,
                &mutated,
                "fixture-invocation-0001",
            )
            .unwrap_err();
            assert_eq!(
                error.details.as_ref().unwrap()["adapterDiagnostic"],
                json!({
                    "schema":"openprose.adapter-diagnostic/1",
                    "adapterId":"prime/rpc",
                    "stage":"prime-lifecycle",
                    "phase":phase,
                    "counters":{
                        "acceptedRecords":accepted,
                        "thinkingDeltas":thinking,
                        "textDeltas":text,
                        "saturated":false,
                    }
                })
            );
            assert!(!serde_json::to_string(&error).unwrap().contains(&canary));
        }

        let mut counter_saturated = false;
        assert_eq!(
            saturating_diagnostic_count(u32::MAX - 1, &mut counter_saturated),
            u32::MAX
        );
        assert!(!counter_saturated);
        assert_eq!(
            saturating_diagnostic_count(u32::MAX, &mut counter_saturated),
            u32::MAX
        );
        assert!(counter_saturated);

        let mut lifecycle_before_framing = records[..14].to_vec();
        lifecycle_before_framing[13]["assistantMessageEvent"]["delta"] =
            json!("candidate-secret-before-later-framing-error");
        assert_eq!(
            prime_parser_diagnostic(&lifecycle_before_framing, "fixture-invocation-0001", true,),
            json!({
                "schema":"openprose.adapter-diagnostic/1",
                "adapterId":"prime/rpc",
                "stage":"prime-lifecycle",
                "phase":"await-text-delta-or-end",
                "counters":{
                    "acceptedRecords":13,
                    "thinkingDeltas":3,
                    "textDeltas":1,
                    "saturated":false,
                }
            })
        );

        let mut enriched_ack = records.clone();
        enriched_ack[0]["extra"] = json!(true);
        let mut enriched_thinking_delta = records.clone();
        enriched_thinking_delta[7]["assistantMessageEvent"]["extra"] = json!(true);
        let mut leaked_partial = records.clone();
        leaked_partial[9]["assistantMessageEvent"]["partial"] = records[9]["message"].clone();
        let mut oversized = records.clone();
        oversized[6]["message"]["responseId"] = json!("x".repeat(65_537));
        for mutated in [
            enriched_ack,
            enriched_thinking_delta,
            leaked_partial,
            oversized,
        ] {
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::PrimeRpc,
                    &mutated,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }

        let mut interactive = records.clone();
        interactive.insert(
            6,
            json!({"type":"extension_ui_request","id":"x","method":"confirm"}),
        );
        assert_eq!(
            normalize_transport(
                InstalledAdapter::PrimeRpc,
                &interactive,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::HarnessFailed
        );
    }

    fn omp_lifecycle() -> Vec<Value> {
        vec![
            json!({
                "type":"ready",
                "protocolVersion":1,
                "supportedProtocolVersions":[1,2],
                "maxFrameBytes":1_048_576,
                "maxReassembledFrameBytes":67_108_864
            }),
            json!({"type":"available_commands_update","commands":[]}),
            omp_state_response(&[]),
            json!({"type":"agent_start"}),
            json!({"type":"turn_start"}),
            json!({"type":"message_start","message":{"role":"user","content":[]}}),
            json!({"type":"message_end","message":{"role":"user","content":[]}}),
            json!({"type":"message_start","message":{"role":"assistant","content":[]}}),
            json!({
                "type":"message_update",
                "assistantMessageEvent":{"type":"text_delta","delta":"echo"},
                "message":{"role":"assistant","content":[]}
            }),
            json!({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"echo"}]}}),
            json!({
                "type":"turn_end",
                "message":{"role":"assistant","content":[{"type":"text","text":"echo"}]},
                "toolResults":[]
            }),
            json!({
                "type":"agent_end",
                "messages":[],
                "telemetry":omp_telemetry(),
                "coverage":omp_coverage(),
                "isTerminal":true
            }),
        ]
    }

    fn omp_response(success: bool) -> Value {
        json!({
            "id":"fixture-invocation-0001.omp.prompt.1",
            "type":"response",
            "command":"prompt",
            "success":success
        })
    }

    fn omp_state_response(tools: &[Value]) -> Value {
        json!({
            "id":"fixture-invocation-0001.omp.state.1",
            "type":"response",
            "command":"get_state",
            "success":true,
            "data":{"dumpTools":tools}
        })
    }

    fn omp_widget() -> Value {
        json!({
            "type":"extension_ui_request",
            "id":"widget-request-0001",
            "method":"setWidget",
            "widgetKey":"autoresearch",
            "widgetLines":["status","running"],
            "widgetPlacement":"aboveEditor"
        })
    }

    fn omp_telemetry() -> Value {
        json!({
            "chats":{"total":1,"byStopReason":{"stop":1},"totalLatencyMs":12.5},
            "tools":{
                "total":0,
                "ok":0,
                "error":0,
                "skipped":0,
                "blocked":0,
                "timeout":0,
                "aborted":0,
                "totalLatencyMs":0,
                "byName":{}
            },
            "usage":{
                "inputTokens":4,
                "outputTokens":2,
                "cachedInputTokens":0,
                "cacheWriteTokens":0,
                "reasoningOutputTokens":0,
                "totalTokens":6
            },
            "cost":{"estimatedUsd":0,"unavailableReasons":["fixture-provider"]},
            "errors":{"total":0,"byType":{}},
            "stepCount":1
        })
    }

    fn omp_coverage() -> Value {
        json!({
            "toolsAvailable":[],
            "toolsInvoked":[],
            "toolsUnused":[],
            "modelsUsed":["fixture-model"],
            "providersUsed":["openrouter"]
        })
    }

    #[test]
    fn omp_ready_and_prompt_ack_ordering_follow_the_upstream_rpc_contract() {
        for response_index in [3, 6, 10, 11, 12] {
            let mut records = omp_lifecycle();
            records.insert(response_index, omp_response(true));
            let normalized = normalize_transport(
                InstalledAdapter::OmpRpc,
                &records,
                "fixture-invocation-0001",
            )
            .unwrap();
            assert_eq!(normalized.terminal_event, "agent_end");
            assert_eq!(normalized.assistant_messages, ["echo"]);
        }

        let mut legacy = omp_lifecycle();
        legacy[0] = json!({"type":"ready"});
        legacy.insert(3, omp_response(true));
        assert!(
            normalize_transport(InstalledAdapter::OmpRpc, &legacy, "fixture-invocation-0001")
                .is_ok()
        );

        let mut agent_end_without_marker = omp_lifecycle();
        agent_end_without_marker
            .last_mut()
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove("isTerminal");
        agent_end_without_marker.insert(3, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &agent_end_without_marker,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        for terminal in [
            json!({"type":"agent_end","messages":[],"isTerminal":true}),
            json!({
                "type":"agent_end",
                "messages":[],
                "telemetry":omp_telemetry(),
                "coverage":omp_coverage(),
                "isTerminal":true
            }),
            json!({
                "type":"agent_end",
                "messages":[],
                "messageCount":2,
                "telemetry":omp_telemetry(),
                "coverage":omp_coverage(),
                "isTerminal":true
            }),
        ] {
            let mut records = omp_lifecycle();
            *records.last_mut().unwrap() = terminal;
            records.insert(3, omp_response(true));
            assert!(normalize_transport(
                InstalledAdapter::OmpRpc,
                &records,
                "fixture-invocation-0001"
            )
            .is_ok());
        }

        for event_type in OMP_ASSISTANT_MESSAGE_EVENTS {
            let mut records = omp_lifecycle();
            records[8]["assistantMessageEvent"]["type"] = json!(event_type);
            records.insert(3, omp_response(true));
            assert!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .is_ok(),
                "{event_type} should be admitted"
            );
        }
    }

    #[test]
    fn omp_rpc_rejects_missing_duplicate_failed_or_uncorrelated_prompt_ack() {
        let missing = omp_lifecycle();
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &missing,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut duplicate = omp_lifecycle();
        duplicate.insert(3, omp_response(true));
        duplicate.insert(8, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &duplicate,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut failed = omp_lifecycle();
        failed.insert(3, omp_response(false));
        assert_eq!(
            normalize_transport(InstalledAdapter::OmpRpc, &failed, "fixture-invocation-0001")
                .unwrap_err()
                .code,
            ErrorCode::HarnessFailed
        );

        let mut uncorrelated = omp_lifecycle();
        let mut response = omp_response(true);
        response["id"] = Value::String("other".to_owned());
        uncorrelated.insert(3, response);
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &uncorrelated,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut extra_field = omp_lifecycle();
        let mut response = omp_response(true);
        response["data"] = json!({"agentInvoked":true});
        extra_field.insert(3, response);
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &extra_field,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );
    }

    #[test]
    fn omp_rpc_requires_one_correlated_successful_empty_tool_state_proof() {
        let mut missing = omp_lifecycle();
        missing.remove(2);
        missing.insert(2, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &missing,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut reordered = omp_lifecycle();
        reordered.insert(2, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &reordered,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut duplicate = omp_lifecycle();
        duplicate.insert(3, omp_state_response(&[]));
        duplicate.insert(4, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &duplicate,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut failed = omp_lifecycle();
        failed[2]["success"] = json!(false);
        failed.insert(3, omp_response(true));
        assert_eq!(
            normalize_transport(InstalledAdapter::OmpRpc, &failed, "fixture-invocation-0001")
                .unwrap_err()
                .code,
            ErrorCode::HarnessFailed
        );

        let mut nonempty = omp_lifecycle();
        nonempty[2] = omp_state_response(&[json!({"name":""})]);
        nonempty.insert(3, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &nonempty,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        for mutated in [
            json!({
                "id":"wrong.omp.state.1","type":"response","command":"get_state",
                "success":true,"data":{"dumpTools":[]}
            }),
            json!({
                "id":"fixture-invocation-0001.omp.state.1","type":"response",
                "command":"get_state","success":true,"data":{}
            }),
            json!({
                "id":"fixture-invocation-0001.omp.state.1","type":"response",
                "command":"get_state","success":true,"data":{"dumpTools":{} }
            }),
        ] {
            let mut records = omp_lifecycle();
            records[2] = mutated;
            records.insert(3, omp_response(true));
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }
    }

    #[test]
    fn omp_rpc_rejects_invalid_ready_lifecycle_interaction_and_post_terminal_records() {
        let mutations = [
            (1, json!({"type":"ready"}), ErrorCode::ProtocolMalformed),
            (
                2,
                json!({"type":"extension_ui_request","id":"ui"}),
                ErrorCode::ProtocolMalformed,
            ),
            (3, json!({"type":"turn_end"}), ErrorCode::ProtocolMalformed),
            (3, json!({"type":"unknown"}), ErrorCode::ProtocolMalformed),
        ];
        for (index, record, expected) in mutations {
            let mut records = omp_lifecycle();
            records.insert(3, omp_response(true));
            records.insert(index, record);
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                expected
            );
        }

        let mut nonterminal = omp_lifecycle();
        nonterminal.insert(3, omp_response(true));
        nonterminal.last_mut().unwrap()["isTerminal"] = Value::Bool(false);
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &nonterminal,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::HarnessFailed
        );
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &nonterminal,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .details
            .unwrap()["reason"],
            "unsupported_nonterminal_settlement"
        );

        for invalid_marker in [json!(null), json!("true"), json!(1)] {
            let mut records = omp_lifecycle();
            records.insert(3, omp_response(true));
            records.last_mut().unwrap()["isTerminal"] = invalid_marker;
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }

        for rejected_event in [
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
            "unknown",
            "",
        ] {
            let mut records = omp_lifecycle();
            records.insert(3, omp_response(true));
            records[9]["assistantMessageEvent"]["type"] = json!(rejected_event);
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }

        let mut missing_commands = omp_lifecycle();
        missing_commands.remove(1);
        missing_commands.insert(1, omp_response(true));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &missing_commands,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let invalid_shapes = [
            (
                1,
                json!({"type":"available_commands_update","commands":[],"extra":true}),
            ),
            (3, json!({"type":"agent_start","extra":true})),
            (4, json!({"type":"turn_start","extra":true})),
            (5, json!({"type":"message_start"})),
            (
                6,
                json!({"type":"message_end","message":{"role":"user"},"extra":true}),
            ),
            (
                7,
                json!({"type":"message_start","message":{"role":"assistant"},"extra":true}),
            ),
            (
                8,
                json!({"type":"message_update","message":{"role":"assistant"}}),
            ),
            (
                9,
                json!({"type":"message_end","message":{"role":"assistant"},"extra":true}),
            ),
            (
                10,
                json!({"type":"turn_end","message":{"role":"assistant"}}),
            ),
            (
                10,
                json!({"type":"turn_end","message":{"role":"assistant"},"toolResults":[{}]}),
            ),
            (11, json!({"type":"agent_end","isTerminal":true})),
            (
                11,
                json!({"type":"agent_end","messages":[],"isTerminal":true,"extra":true}),
            ),
            (
                11,
                json!({"type":"agent_end","messages":[],"telemetry":omp_telemetry()}),
            ),
            (
                11,
                json!({"type":"agent_end","messages":[],"coverage":omp_coverage()}),
            ),
            (
                11,
                json!({"type":"agent_end","messages":[],"telemetry":{},"coverage":omp_coverage()}),
            ),
            (
                11,
                json!({"type":"agent_end","messages":[{}],"messageCount":0}),
            ),
            (
                11,
                json!({"type":"agent_end","messages":[],"messageCount":-1}),
            ),
            (
                11,
                json!({
                    "type":"agent_end",
                    "messages":[],
                    "telemetry":omp_telemetry(),
                    "coverage":{
                        "toolsAvailable":[],
                        "toolsInvoked":[],
                        "toolsUnused":[],
                        "modelsUsed":["z","a"],
                        "providersUsed":[]
                    }
                }),
            ),
        ];
        for (index, record) in invalid_shapes {
            let mut records = omp_lifecycle();
            records[index] = record;
            records.insert(3, omp_response(true));
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }

        let mut too_many = omp_lifecycle();
        too_many.insert(3, omp_response(true));
        too_many.last_mut().unwrap()["coverage"]["toolsAvailable"] = Value::Array(
            (0..=OMP_TERMINAL_COLLECTION_LIMIT)
                .map(|index| Value::String(format!("tool-{index:04}")))
                .collect(),
        );
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &too_many,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );

        let mut post_terminal = omp_lifecycle();
        post_terminal.insert(3, omp_response(true));
        post_terminal
            .push(json!({"type":"response","id":"late","command":"prompt","success":true}));
        assert_eq!(
            normalize_transport(
                InstalledAdapter::OmpRpc,
                &post_terminal,
                "fixture-invocation-0001"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );
    }

    #[test]
    fn omp_rpc_accepts_exact_set_widget_presentation_across_lifecycle_and_settlement() {
        let supervisor_protocol = InstalledAdapter::OmpRpc.protocol();
        assert!(supervisor_protocol
            .allowed_events
            .contains("extension_ui_request"));
        assert!(!supervisor_protocol
            .failure_events
            .contains("extension_ui_request"));
        assert!(supervisor_protocol
            .allowed_after_terminal_events
            .contains("extension_ui_request"));

        let base = omp_lifecycle();
        let turn_end = base
            .iter()
            .position(|record| record_type(record) == Some("turn_end"))
            .unwrap();
        let agent_end = base
            .iter()
            .position(|record| record_type(record) == Some("agent_end"))
            .unwrap();

        for index in [1, turn_end + 1, agent_end + 1] {
            let mut records = base.clone();
            records.insert(3, omp_response(true));
            let adjusted = if index >= 3 { index + 1 } else { index };
            records.insert(adjusted, omp_widget());
            let normalized = normalize_transport(
                InstalledAdapter::OmpRpc,
                &records,
                "fixture-invocation-0001",
            )
            .unwrap();
            assert_eq!(normalized.terminal_event, "agent_end");
            assert_eq!(normalized.assistant_messages, ["echo"]);
        }

        let mut cleared = base;
        cleared.insert(
            1,
            json!({
                "type":"extension_ui_request",
                "id":"widget-request-0002",
                "method":"setWidget",
                "widgetKey":"autoresearch"
            }),
        );
        cleared.insert(4, omp_response(true));
        assert!(normalize_transport(
            InstalledAdapter::OmpRpc,
            &cleared,
            "fixture-invocation-0001"
        )
        .is_ok());

        for presentation in [
            json!({"type":"extension_ui_request","id":"notify","method":"notify","message":"status","notifyType":"info"}),
            json!({"type":"extension_ui_request","id":"status","method":"setStatus","statusKey":"run","statusText":"active"}),
            json!({"type":"extension_ui_request","id":"title","method":"setTitle","title":"Run"}),
            json!({"type":"extension_ui_request","id":"editor","method":"set_editor_text","text":"presentation only"}),
        ] {
            let mut records = omp_lifecycle();
            records.insert(3, omp_response(true));
            records.insert(3, presentation);
            assert!(normalize_transport(
                InstalledAdapter::OmpRpc,
                &records,
                "fixture-invocation-0001"
            )
            .is_ok());
        }
    }

    #[test]
    fn omp_rpc_rejects_interactive_malformed_unknown_and_oversized_ui_requests() {
        for method in ["select", "confirm", "input", "editor", "open_url"] {
            let mut records = omp_lifecycle();
            records.insert(3, omp_response(true));
            records.insert(
                3,
                json!({"type":"extension_ui_request","id":"ui","method":method}),
            );
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::HarnessFailed
            );
        }

        let invalid = [
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget"}),
            json!({"type":"extension_ui_request","id":"","method":"setWidget","widgetKey":"widget"}),
            json!({"type":"extension_ui_request","id":"x".repeat(OMP_TERMINAL_STRING_BYTES_LIMIT + 1),"method":"setWidget","widgetKey":"widget"}),
            json!({"type":"extension_ui_request","id":"ui","method":"set_widget","widgetKey":"widget"}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"widget","extra":true}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":1}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"x".repeat(OMP_TERMINAL_STRING_BYTES_LIMIT + 1)}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"widget","widgetLines":null}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"widget","widgetLines":["ok",1]}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"widget","widgetLines":vec!["line"; OMP_TERMINAL_COLLECTION_LIMIT + 1]}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"widget","widgetLines":["x".repeat(OMP_TERMINAL_STRING_BYTES_LIMIT + 1)]}),
            json!({"type":"extension_ui_request","id":"ui","method":"setWidget","widgetKey":"widget","widgetPlacement":"sidebar"}),
        ];
        for frame in invalid {
            let mut records = omp_lifecycle();
            records.insert(3, omp_response(true));
            records.insert(3, frame);
            assert_eq!(
                normalize_transport(
                    InstalledAdapter::OmpRpc,
                    &records,
                    "fixture-invocation-0001"
                )
                .unwrap_err()
                .code,
                ErrorCode::ProtocolMalformed
            );
        }
    }

    #[test]
    fn transport_state_machines_reject_failure_duplicates_and_identity_drift() {
        let codex = vec![
            json!({"type":"thread.started","thread_id":"one"}),
            json!({"type":"turn.started"}),
            json!({"type":"turn.failed"}),
            json!({"type":"turn.completed"}),
        ];
        assert_eq!(
            normalize_transport(InstalledAdapter::CodexExecJson, &codex, "unused")
                .unwrap_err()
                .code,
            ErrorCode::HarnessFailed
        );
        let duplicate_turn = vec![
            json!({"type":"thread.started","thread_id":"one"}),
            json!({"type":"turn.started"}),
            json!({"type":"turn.started"}),
            json!({"type":"turn.completed"}),
        ];
        assert_eq!(
            normalize_transport(InstalledAdapter::CodexExecJson, &duplicate_turn, "unused")
                .unwrap_err()
                .code,
            ErrorCode::ProtocolMalformed
        );
        let claude = vec![
            json!({"type":"system","subtype":"init","session_id":"one"}),
            json!({"type":"assistant","session_id":"two","message":{"role":"assistant","content":[]}}),
            json!({"type":"result","subtype":"success","session_id":"one","is_error":false}),
        ];
        assert_eq!(
            normalize_transport(InstalledAdapter::ClaudePrintStreamJson, &claude, "unused")
                .unwrap_err()
                .code,
            ErrorCode::ProtocolMalformed
        );
        let claude_wrong_role = vec![
            json!({"type":"system","subtype":"init","session_id":"one"}),
            json!({"type":"assistant","session_id":"one","message":{"role":"user","content":[]}}),
            json!({"type":"result","subtype":"success","session_id":"one","is_error":false}),
        ];
        assert_eq!(
            normalize_transport(
                InstalledAdapter::ClaudePrintStreamJson,
                &claude_wrong_role,
                "unused"
            )
            .unwrap_err()
            .code,
            ErrorCode::ProtocolMalformed
        );
        let rpc_failure = vec![
            json!({"type":"response","id":"fixture","command":"prompt","success":false}),
            json!({"type":"agent_start"}),
            json!({"type":"agent_end"}),
        ];
        assert_eq!(
            normalize_transport(InstalledAdapter::PrimeRpc, &rpc_failure, "fixture")
                .unwrap_err()
                .code,
            ErrorCode::HarnessFailed
        );
    }

    #[test]
    fn terminal_recovery_preserves_visible_assistant_boundaries() {
        let expected = vec!["prose".to_owned(), "run".to_owned(), "hello".to_owned()];
        let terminal = format!(
            "second\n{{\"schema\":\"openprose.echo-terminal/1\",\"semanticStatus\":\"not-applicable\",\"placeholder\":true,\"marker\":\"OPENPROSE_ECHO_TERMINAL_V0\",\"task\":{{\"argv\":{}}}}}",
            serde_json::to_string(&expected).unwrap()
        );
        let recovered =
            recover_terminal(&echo_image(), &["first".to_owned(), terminal], &expected).unwrap();
        assert_eq!(recovered.visible_messages, ["first", "second"]);
        assert_eq!(recovered.visible_text, "first\nsecond");
    }
    #[test]
    fn claude_thinking_tokens_are_information_not_completion() {
        let telemetry: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/claude-thinking-tokens.json"
        ))
        .unwrap();
        let init = json!({"type":"system","subtype":"init","session_id":"fixture-session"});
        let done = json!({"type":"result","subtype":"success","is_error":false,"session_id":"fixture-session"});
        let adapter = InstalledAdapter::ClaudePrintStreamJson;
        assert!(normalize_transport(
            adapter,
            &[init.clone(), telemetry.clone(), done.clone()],
            "unused"
        )
        .is_ok());
        assert!(
            normalize_transport(adapter, &[init.clone(), telemetry.clone()], "unused").is_err()
        );
        for (key, value) in [
            ("session_id", json!("other")),
            ("estimated_tokens", json!(-1)),
            ("estimated_tokens_delta", json!(0.5)),
            ("uuid", json!("")),
            ("unexpected", json!(true)),
        ] {
            let mut invalid = telemetry.clone();
            invalid[key] = value;
            assert!(
                normalize_transport(adapter, &[init.clone(), invalid, done.clone()], "unused")
                    .is_err()
            );
        }
    }
    #[test]
    fn claude_api_profile_is_explicit_and_requires_key() {
        let adapter = InstalledAdapter::ClaudePrintStreamJson;
        assert_eq!(credential_names(adapter,"anthropic-api-key"),Some(["ANTHROPIC_API_KEY"].as_slice()));
        assert!(auth_readiness(adapter,"anthropic-api-key",&[]).is_err());
        assert_eq!(auth_readiness(adapter,"anthropic-api-key",&[("ANTHROPIC_API_KEY".into(),"fixture-key".into())]).unwrap(),"unknown");
        assert_eq!(credential_names(adapter,"claude-subscription"),Some([].as_slice()));
        let root = TempDir::new().unwrap();
        for group in ["anthropic-api-key", "claude-subscription"] {
            let launch = prepare_launch(adapter,root.path().join("claude"),root.path(),&full_image(),FRAMING,TASK.as_bytes(),"fixture",None,group,empty_environment(adapter)).unwrap();
            assert_eq!(launch.argv.iter().any(|arg| arg == "--bare"),group == "anthropic-api-key");
        }
    }

    #[test]
    fn workspace_profile_launch_matches_shared_fixture(){
        let fixture:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/native-profile.json")).unwrap();
        let root=TempDir::new().unwrap();let adapter=InstalledAdapter::ClaudePrintStreamJson;
        for group in ["anthropic-api-key","claude-subscription"] {
            let mut launch=prepare_launch(adapter,root.path().join("claude"),root.path(),&full_image(),FRAMING,TASK.as_bytes(),"fixture",None,group,empty_environment(adapter)).unwrap();
            launch.apply_workspace_profile(group,&[],&[]).unwrap();
            let flags:Vec<_>=fixture["flags"].as_array().unwrap().iter().map(|x|OsString::from(x.as_str().unwrap())).collect();assert_eq!(&launch.argv[..flags.len()],flags.as_slice());
            assert!(!launch.argv.iter().any(|x|x=="--bare" || x=="--allowedTools" || x=="--add-dir"));
            assert!(launch.argv.iter().any(|x|x=="--safe-mode"));
            assert_eq!(launch.credential_config_directory().is_some(),group=="anthropic-api-key");
            let owned=launch.credential_config_directory().map(Path::to_owned);launch.finalize_private_files().unwrap();if let Some(path)=owned {assert!(!path.exists());}
        }
        let mut launch=prepare_launch(adapter,root.path().join("claude"),root.path(),&full_image(),FRAMING,TASK.as_bytes(),"fixture",None,"claude-subscription",empty_environment(adapter)).unwrap();
        launch.apply_workspace_profile("claude-subscription",&["/tmp/a b".into()],&["Bash(git status:*)".into(),"Agent".into()]).unwrap();
        assert!(launch.argv.windows(2).any(|x|x==[OsString::from("--add-dir"),OsString::from("/tmp/a b")]));
        assert_eq!(launch.argv.iter().filter(|x|*x=="--allowedTools").count(),2);
    }

    #[test]
    fn claude_permission_denial_remains_nonterminal() {
        let init=json!({"type":"system","subtype":"init","session_id":"s"});
        let denial=json!({"type":"system","subtype":"permission_denied","session_id":"s","tool_name":"Bash","tool_use_id":"t","message":"Denied"});
        let final_record=json!({"type":"result","subtype":"success","is_error":false,"session_id":"s"});
        let adapter=InstalledAdapter::ClaudePrintStreamJson;
        assert!(normalize_transport(adapter,&[init.clone(),denial.clone(),final_record.clone()],"unused").is_ok());
        assert!(normalize_transport(adapter,&[init.clone(),denial.clone()],"unused").is_err());
        let mut invalid=denial;invalid["session_id"]=json!("other");
        assert!(normalize_transport(adapter,&[init,invalid,final_record],"unused").is_err());
    }

}

#[test]
fn agents_sdk_native_transport_requires_start_and_terminal(){
 let start=serde_json::json!({"type":"start","model":"fixture","cwd":"/tmp"});
 let mut records=vec![start,serde_json::json!({"type":"tool_call","name":"execute_shell"}),serde_json::json!({"type":"tool_result","name":"execute_shell"})];
 assert!(normalize_transport(InstalledAdapter::AgentsSdkJsonl,&records,"fixture").is_err());
 records.push(serde_json::json!({"type":"final","output":"All done, no JSON."}));
 assert_eq!(normalize_transport(InstalledAdapter::AgentsSdkJsonl,&records,"fixture").unwrap().assistant_messages,vec!["All done, no JSON."]);
 records.push(serde_json::json!({"type":"final","output":"duplicate"}));
 assert!(normalize_transport(InstalledAdapter::AgentsSdkJsonl,&records,"fixture").is_err());
}

#[test]
fn claude_native_task_lifecycle_never_settles_outer_invocation(){
 let tasks:Vec<Value>=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/claude-task-lifecycle.json")).unwrap();
 let init=serde_json::json!({"type":"system","subtype":"init","session_id":"fixture-session"});
 let done=serde_json::json!({"type":"result","subtype":"success","is_error":false,"session_id":"fixture-session"});
 let adapter=InstalledAdapter::ClaudePrintStreamJson;
 let mut records=vec![init.clone()];records.extend(tasks.clone());
 assert!(normalize_transport(adapter,&records,"unused").is_err());records.push(done.clone());assert!(normalize_transport(adapter,&records,"unused").is_ok());
 for task in tasks {
  for (key,value) in [("session_id",serde_json::json!("other")),("task_id",serde_json::json!("")),("subtype",serde_json::json!("task_invented"))]{
   let mut invalid=task.clone();invalid[key]=value;assert!(normalize_transport(adapter,&[init.clone(),invalid,done.clone()],"unused").is_err());
  }
 }
}
#[test]
fn claude_background_inventory_is_nonterminal(){
 let event:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/claude-background-tasks.json")).unwrap();
 let init=serde_json::json!({"type":"system","subtype":"init","session_id":"fixture-session"});let done=serde_json::json!({"type":"result","subtype":"success","is_error":false,"session_id":"fixture-session"});let a=InstalledAdapter::ClaudePrintStreamJson;
 assert!(normalize_transport(a,&[init.clone(),event.clone()],"unused").is_err());assert!(normalize_transport(a,&[init.clone(),event.clone(),done.clone()],"unused").is_ok());let mut bad=event;bad["tasks"]=serde_json::json!([{}]);assert!(normalize_transport(a,&[init,bad,done],"unused").is_err());
}
#[test]
fn claude_task_resumption_preserves_initial_configuration(){
 let init=serde_json::json!({"type":"system","subtype":"init","session_id":"fixture-session","uuid":"initial"});let notice=serde_json::json!({"type":"system","subtype":"task_notification","session_id":"fixture-session","uuid":"n","task_id":"t","status":"completed"});let done=serde_json::json!({"type":"result","subtype":"success","is_error":false,"session_id":"fixture-session"});let mut repeated=init.clone();repeated["uuid"]=serde_json::json!("resumed");let a=InstalledAdapter::ClaudePrintStreamJson;
 assert!(normalize_transport(a,&[init.clone(),notice.clone(),repeated.clone(),done.clone()],"unused").is_ok());assert!(normalize_transport(a,&[init.clone(),repeated.clone(),done.clone()],"unused").is_err());repeated["cwd"]=serde_json::json!("changed");assert!(normalize_transport(a,&[init,notice,repeated,done],"unused").is_err());
}

#[cfg(test)]
mod native_turn_tests {
 use super::*;
 #[test]
 fn native_claude_turns_need_final_success_and_legacy_rejects_duplicates(){
  let records:Vec<Value>=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/claude-native-turns.json")).unwrap();
  let run=|r:&[Value]|normalize_transport_mode(InstalledAdapter::ClaudePrintStreamJson,r,"fixture",true);
  assert!(run(&records).is_ok());assert!(normalize_transport(InstalledAdapter::ClaudePrintStreamJson,&records,"fixture").is_err());
  let mut more=records.clone();more.push(json!({"type":"assistant","session_id":"fixture-session","message":{"role":"assistant","content":[]}}));assert!(run(&more).is_err());
  more.push(records[2].clone());assert!(run(&more).is_ok());
  for change in [json!({"session_id":"other"}),json!({"is_error":true}),json!({"type":"unsupported"})] {
   let mut bad=records.clone();for(k,v)in change.as_object().unwrap(){bad[2][k]=v.clone();}assert!(run(&bad).is_err());
  }
  assert!(run(&records[..1]).is_err());
 }
 #[test]
 #[ignore="set CLAUDE_REPLAY_PATH to a retained native trace"]
 fn recorded_claude_native_turn_replay(){
  let text=std::fs::read_to_string(std::env::var("CLAUDE_REPLAY_PATH").unwrap()).unwrap();let records:Vec<Value>=text.lines().map(|l|serde_json::from_str(l).unwrap()).collect();
  assert_eq!(normalize_transport_mode(InstalledAdapter::ClaudePrintStreamJson,&records,"fixture",true).is_ok(),std::env::var("CLAUDE_REPLAY_EXPECT_COMPLETE").as_deref()==Ok("true") || records.last().unwrap()["type"]=="result");
  if records.last().unwrap()["type"]!="result" { let mut synthetic=records.clone();synthetic.push(json!({"type":"result","subtype":"success","is_error":false,"session_id":records[0]["session_id"]}));assert!(normalize_transport_mode(InstalledAdapter::ClaudePrintStreamJson,&synthetic,"fixture",true).is_ok()); }
 }
}

#[cfg(test)]
#[test]
fn native_repeated_init_requires_metadata_identity_and_fresh_result(){
 let init=json!({"type":"system","subtype":"init","session_id":"fixture","uuid":"one","tools":["Read"],"model":"fixture-model","apiKeySource":"fixture-auth"});
 let result=json!({"type":"result","subtype":"success","is_error":false,"session_id":"fixture"});
 let mut repeated=init.clone();repeated["uuid"]=json!("two");let records=vec![init.clone(),result.clone(),repeated.clone(),result.clone()];
 let run=|r:&[Value]|normalize_transport_mode(InstalledAdapter::ClaudePrintStreamJson,r,"fixture",true);
 assert!(run(&records).is_ok());assert!(run(&records[..3]).is_err());assert!(normalize_transport(InstalledAdapter::ClaudePrintStreamJson,&records,"fixture").is_err());
 for (key,value) in [("uuid",json!("")),("tools",json!(["Write"])),("model",json!("other")),("apiKeySource",json!("other")),("session_id",json!("other"))]{let mut bad=records.clone();bad[2][key]=value;assert!(run(&bad).is_err());}
}

#[cfg(test)]
#[test]
fn native_routing_metadata_mutation_is_typed_and_not_identity(){
 let init=json!({"type":"system","subtype":"init","session_id":"fixture","uuid":"one"});let result=json!({"type":"result","subtype":"success","is_error":false,"session_id":"fixture"});
 let run=|r:&[Value]|normalize_transport_mode(InstalledAdapter::ClaudePrintStreamJson,r,"fixture",true);
 for initial in [None,Some("/tmp/old")] {for next in [None,Some("/tmp/new")] {let mut first=init.clone();let mut repeated=init.clone();repeated["uuid"]=json!("two");if let Some(v)=initial{first["messaging_socket_path"]=json!(v);}if let Some(v)=next{repeated["messaging_socket_path"]=json!(v);}let records=vec![first,result.clone(),repeated,result.clone()];assert!(run(&records).is_ok());assert!(run(&records[..3]).is_err());}}
 for bad in [json!(null),json!(0),json!({}),json!("")] {let mut invalid=init.clone();invalid["messaging_socket_path"]=bad;assert!(run(&[invalid.clone(),result.clone()]).is_err());assert!(run(&[init.clone(),invalid,result.clone()]).is_err());}
}

#[cfg(test)]
#[test]
fn claude_correlated_shutdown_requires_closed_known_native_tasks(){
 let records:Vec<Value>=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/claude-shutdown.json")).unwrap();
 let run=|r:&[Value]|normalize_transport_mode(InstalledAdapter::ClaudePrintStreamJson,r,"fixture",true);
 assert!(run(&records).is_ok());assert!(normalize_transport(InstalledAdapter::ClaudePrintStreamJson,&records,"fixture").is_err());
 let mut variants=Vec::new();
 for (index,key,value) in [(7,"task_id",json!("other")),(7,"tool_use_id",json!("other")),(7,"session_id",json!("other")),(2,"task_type",json!("agent")),(5,"tasks",json!([{"task_id":"new","description":"new","task_type":"local_bash"}]))]{let mut bad=records.clone();bad[index][key]=value;variants.push(bad);}
 let mut bad=records.clone();bad.pop();variants.push(bad);
 for event in [json!({"type":"assistant","session_id":"fixture-session","message":{"role":"assistant","content":[]}}),json!({"type":"tool_progress","session_id":"fixture-session"})]{let mut bad=records.clone();bad.push(event);variants.push(bad);}
 let mut bad=records.clone();bad[6]["patch"]["end_time"]=json!(-1);variants.push(bad);
 for bad in variants{assert!(run(&bad).is_err());}
}

pub(crate) fn sdk_native_failure(record: &Value)->Value {
 let kind=match record.get("error_type").and_then(Value::as_str) {Some("MaxTurnsExceeded")=>"max-turns",Some("TimeoutError")=>"timeout",_=>"execution"};
 let mut result=json!({"kind":kind});
 if let Some(n)=record.get("elapsed_seconds").and_then(Value::as_f64).filter(|n|n.is_finite() && *n>=0.0) {result["elapsedSeconds"]=json!(n);}
 if let Some(l)=record.get("limits").filter(|l|l.as_object().is_some_and(|m|m.len()==4)) {
  let ints=["maxTurns","maxOutputTokens"].iter().all(|k|l.get(k).and_then(Value::as_f64).is_some_and(|n|n.is_finite() && n>0.0 && n.fract()==0.0 && n<=9_007_199_254_740_991.0));
  let times=["timeoutSeconds","toolTimeoutSeconds"].iter().all(|k|l.get(k).and_then(Value::as_f64).is_some_and(|n|n.is_finite() && n>0.0 && n<=9_007_199_254_740.991));
  if ints && times {result["limits"]=l.clone();}
 }
 result
}

#[cfg(test)]
mod sdk_budget_diagnostic_tests {
 use super::*;
 #[test]
 fn native_failure_is_closed_and_validated() {
  let f:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/sdk-native-limits.json")).unwrap();
  for case in f["errorCases"].as_array().unwrap() {
   let record=json!({"error_type":case["error_type"],"limits":f["defaults"],"elapsed_seconds":1.5});
   let d=sdk_native_failure(&record);assert_eq!(d["kind"],case["kind"]);assert_eq!(d["limits"],f["defaults"]);assert_eq!(d["elapsedSeconds"],1.5);assert!(!d.to_string().contains("Untrusted"));
  }
  let err=normalize_transport(InstalledAdapter::AgentsSdkJsonl,&[json!({"type":"start","model":"fixture","cwd":"/fixture"}),json!({"type":"error","error_type":"MaxTurnsExceeded","limits":f["defaults"]})],"fixture").unwrap_err();
  assert_eq!(serde_json::to_value(err).unwrap()["details"]["nativeFailure"]["kind"],"max-turns");
  let d=sdk_native_failure(&json!({"error_type":"secret","elapsed_seconds":-1,"limits":{"maxTurns":20,"timeoutSeconds":180,"toolTimeoutSeconds":30,"maxOutputTokens":12000,"extra":"secret"}}));
  assert_eq!(d,json!({"kind":"execution"}));
 }
}

#[cfg(test)]
mod prime_child_outer_tests {
 use super::*;
 #[test]
 fn prime_child_telemetry_reaches_typed_admission() {
  let protocol=InstalledAdapter::PrimeRpc.protocol();
  assert!(protocol.allowed_events.contains("rlm_child_update"));
  assert!(protocol.allowed_events.contains("session_action_update"));
  assert_ne!(protocol.terminal_event,"rlm_child_update");
  assert!(!InstalledAdapter::OmpRpc.protocol().allowed_events.contains("rlm_child_update"));
 }
}
