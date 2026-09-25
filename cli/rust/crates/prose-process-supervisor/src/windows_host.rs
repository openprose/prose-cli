//! Host-neutral client contract for the native Windows process-containment host.
//!
//! This module deliberately handles only process bytes and lifecycle evidence.
//! Decoded child stdout remains opaque for the existing harness-specific JSONL
//! parser, and no `OpenProse` language input is interpreted here.

use crate::FailureKind;
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::BTreeSet;
use std::fmt::{self, Display, Formatter};
use std::fs::{self, File, OpenOptions};
use std::io::Read as _;
use std::path::{Path, PathBuf};

use crate::WINDOWS_PROCESS_HOST_FILENAME;

const REQUEST_SCHEMA: &str = "openprose.windows-process-host.request/1";
const CONTROL_SCHEMA: &str = "openprose.windows-process-host.control/1";
const EVENT_SCHEMA: &str = "openprose.windows-process-host.event/1";
const ERROR_SCHEMA: &str = "openprose.windows-process-host.error/1";
const IDENTITY_SCHEMA: &str = "openprose.windows-process-host.identity/1";
const MAX_REQUEST_BYTES: usize = 64 * 1_048_576;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_PATH_CHARS: usize = 32_766;
const MAX_ARGV_ITEMS: usize = 64;
const MAX_ARGUMENT_CHARS: usize = 32_766;
const MAX_COMMAND_LINE_UNITS: usize = 32_766;
const MAX_ENVIRONMENT_ITEMS: usize = 64;
const MAX_ENVIRONMENT_NAME_CHARS: usize = 128;
const MAX_ENVIRONMENT_VALUE_CHARS: usize = 8192;
// The host schema's base64 maxLength is 22_369_620. Keeping the decoded cap
// divisible by three makes the encoded and decoded limits exactly equivalent.
const MAX_CHILD_STDIN_BYTES: usize = 16_777_215;
const MAX_ENVIRONMENT_BYTES: usize = 4 * 1_048_576;
const MAX_HOST_EVENT_BYTES: usize = 64 * 1024;
const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;
const MAX_QUEUED_CHUNKS: usize = 1024;
const MAX_RUN_TIMEOUT_MS: u64 = 86_400_000;
const MAX_CANCELLATION_MS: u64 = 60_000;
const REQUIRED_METADATA: [&str; 3] = [
    "OPENPROSE_INVOCATION_ID",
    "OPENPROSE_RECURSION_TOKEN",
    "OPENPROSE_RUN_NONCE",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowsGracefulControl {
    CtrlBreak,
    None,
}

impl WindowsGracefulControl {
    const fn label(self) -> &'static str {
        match self {
            Self::CtrlBreak => "ctrl-break",
            Self::None => "none",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowsHostLimits {
    pub max_stdout_bytes: usize,
    pub max_stderr_bytes: usize,
    pub max_queued_chunks: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowsHostCancellation {
    pub graceful: WindowsGracefulControl,
    pub grace_ms: u64,
    pub hard_kill_after_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsHostEnvironmentEntry {
    pub name: String,
    pub value: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsHostRequest {
    pub request_id: String,
    pub executable: String,
    pub wrapper_executable: String,
    pub argv: Vec<String>,
    pub cwd: String,
    pub environment: Vec<WindowsHostEnvironmentEntry>,
    pub child_stdin: Vec<u8>,
    pub limits: WindowsHostLimits,
    pub cancellation: WindowsHostCancellation,
    pub run_timeout_ms: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowsHostTermination {
    Natural,
    Cancelled,
    ParentDisconnected,
    Timeout,
    OutputLimit,
    IoFailure,
    ProtocolFailure,
    Backpressure,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WindowsHostChunk {
    Started { pid: u32 },
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(clippy::struct_excessive_bools)] // Mirrors the closed host evidence record exactly.
pub struct WindowsHostCompletion {
    pub pid: u32,
    pub process_exit_code: u32,
    pub termination: WindowsHostTermination,
    pub graceful_control_attempted: bool,
    pub graceful_control_delivered: bool,
    pub hard_kill_used: bool,
    pub active_processes_before_cleanup: u32,
    pub active_processes_after_cleanup: u32,
    pub cleanup_verified: bool,
    pub stdout_bytes: u64,
    pub stderr_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsHostBootstrapError {
    pub code: String,
    pub operation: String,
    pub win32: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsHostIdentity {
    pub component_version: String,
    pub architecture: String,
}

/// A digest-verified sibling whose open handle prevents replacement before spawn.
#[derive(Debug)]
pub struct VerifiedWindowsHost {
    path: PathBuf,
    _integrity_guard: File,
}

impl VerifiedWindowsHost {
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl WindowsHostBootstrapError {
    /// Maps an authenticated helper bootstrap code into the existing runner taxonomy.
    ///
    /// Unknown codes remain internal failures; there is no permissive fallback.
    #[must_use]
    pub fn supervisor_kind(&self) -> FailureKind {
        match self.code.as_str() {
            "RECURSIVE_INVOCATION" => FailureKind::RecursiveInvocation,
            "CREATE_PROCESS_FAILED" => FailureKind::HarnessUnavailable,
            "PLATFORM_UNSUPPORTED"
            | "JOB_ASSIGNMENT_FAILED"
            | "JOB_CONFIGURATION_FAILED"
            | "JOB_CREATION_FAILED" => FailureKind::ContainmentUnsupported,
            _ => FailureKind::Internal,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowsHostClientFailureKind {
    RequestInvalid,
    RequestLimit,
    ProtocolMalformed,
    ProtocolTruncated,
    StdoutLimit,
    StderrLimit,
    HostDiagnosticMalformed,
    HostDiagnosticLimit,
    HostDisconnected,
    HostFailed,
    HostUnavailable,
    HostIntegrity,
    CleanupUnverified,
    Cancelled,
    TimedOut,
    VersionProbeInvalid,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsHostClientFailure {
    pub kind: WindowsHostClientFailureKind,
    message: &'static str,
}

impl WindowsHostClientFailure {
    const fn new(kind: WindowsHostClientFailureKind, message: &'static str) -> Self {
        Self { kind, message }
    }

    #[must_use]
    pub const fn supervisor_kind(&self) -> FailureKind {
        match self.kind {
            WindowsHostClientFailureKind::ProtocolMalformed
            | WindowsHostClientFailureKind::HostDiagnosticMalformed => {
                FailureKind::ProtocolMalformed
            }
            WindowsHostClientFailureKind::ProtocolTruncated => FailureKind::ProtocolTruncated,
            WindowsHostClientFailureKind::StdoutLimit => FailureKind::ProtocolMalformed,
            WindowsHostClientFailureKind::StderrLimit
            | WindowsHostClientFailureKind::HostFailed => FailureKind::HarnessFailed,
            WindowsHostClientFailureKind::HostUnavailable
            | WindowsHostClientFailureKind::HostIntegrity => FailureKind::ContainmentUnsupported,
            WindowsHostClientFailureKind::CleanupUnverified => FailureKind::CleanupFailed,
            WindowsHostClientFailureKind::Cancelled => FailureKind::Cancelled,
            WindowsHostClientFailureKind::TimedOut => FailureKind::RunTimeout,
            WindowsHostClientFailureKind::RequestInvalid
            | WindowsHostClientFailureKind::RequestLimit
            | WindowsHostClientFailureKind::HostDiagnosticLimit
            | WindowsHostClientFailureKind::HostDisconnected
            | WindowsHostClientFailureKind::VersionProbeInvalid => FailureKind::Internal,
        }
    }
}

/// Resolves only the fixed sibling host, rejects links/non-files, and verifies
/// its exact bytes while retaining an open anti-replacement handle.
///
/// # Errors
///
/// Returns a closed availability or integrity failure. No candidate path or
/// digest is included in the diagnostic.
pub fn verify_windows_host_sibling(
    wrapper_executable: &Path,
    expected_sha256: &str,
) -> Result<VerifiedWindowsHost, WindowsHostClientFailure> {
    if expected_sha256.len() != 64
        || !expected_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(host_integrity());
    }
    let wrapper = fs::canonicalize(wrapper_executable).map_err(|_| host_unavailable())?;
    if !wrapper.is_file() {
        return Err(host_unavailable());
    }
    let directory = wrapper.parent().ok_or_else(host_unavailable)?;
    let candidate = directory.join(WINDOWS_PROCESS_HOST_FILENAME);
    let link_metadata = fs::symlink_metadata(&candidate).map_err(|_| host_unavailable())?;
    if link_metadata.file_type().is_symlink() || !link_metadata.file_type().is_file() {
        return Err(host_integrity());
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt as _;
        // Share read access with CreateProcess, but deny mutation/deletion
        // until the verified helper has been spawned.
        options.share_mode(0x0000_0001);
    }
    let mut file = options.open(&candidate).map_err(|_| host_unavailable())?;
    if !file
        .metadata()
        .map_err(|_| host_unavailable())?
        .file_type()
        .is_file()
    {
        return Err(host_integrity());
    }
    let canonical = fs::canonicalize(&candidate).map_err(|_| host_unavailable())?;
    if canonical.parent() != Some(directory) || canonical == wrapper {
        return Err(host_integrity());
    }
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 16 * 1024];
    loop {
        let count = file.read(&mut buffer).map_err(|_| host_integrity())?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    let actual = format!("{:x}", hasher.finalize());
    if actual != expected_sha256 {
        return Err(host_integrity());
    }
    Ok(VerifiedWindowsHost {
        path: canonical,
        _integrity_guard: file,
    })
}

impl Display for WindowsHostClientFailure {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for WindowsHostClientFailure {}

#[derive(Debug)]
pub struct WindowsHostClient {
    request_id: String,
    limits: WindowsHostLimits,
    next_sequence: u64,
    pid: Option<u32>,
    stdout_bytes: u64,
    stderr_bytes: u64,
    terminal: Option<WindowsHostCompletion>,
    failed: bool,
}

impl WindowsHostClient {
    /// Creates a closed decoder for exactly one host request.
    ///
    /// # Errors
    ///
    /// Returns a request error for an invalid correlation identifier or limit.
    pub fn new(
        request_id: impl Into<String>,
        limits: WindowsHostLimits,
    ) -> Result<Self, WindowsHostClientFailure> {
        let request_id = request_id.into();
        validate_identifier(&request_id).map_err(|()| request_invalid())?;
        validate_limits(limits).map_err(|()| request_invalid())?;
        Ok(Self {
            request_id,
            limits,
            next_sequence: 0,
            pid: None,
            stdout_bytes: 0,
            stderr_bytes: 0,
            terminal: None,
            failed: false,
        })
    }

    /// Returns the validated terminal evidence, if it has been observed.
    #[must_use]
    pub const fn terminal_completion(&self) -> Option<&WindowsHostCompletion> {
        self.terminal.as_ref()
    }

    /// Reports whether a malformed record permanently poisoned this decoder.
    #[must_use]
    pub const fn is_poisoned(&self) -> bool {
        self.failed
    }

    /// Accepts one complete host JSONL record.
    ///
    /// A terminal event is retained internally and returns `None`; callers must
    /// call [`Self::finish`] with the host process status before treating the
    /// child tree as settled.
    ///
    /// # Errors
    ///
    /// Returns a closed protocol or stream-limit failure and permanently
    /// poisons this decoder.
    pub fn accept_line(
        &mut self,
        line: &[u8],
    ) -> Result<Option<WindowsHostChunk>, WindowsHostClientFailure> {
        if self.failed {
            return Err(protocol_malformed());
        }
        let result = self.accept_line_inner(line);
        if result.is_err() {
            self.failed = true;
        }
        result
    }

    /// Settles the host process and requires authoritative empty-Job evidence.
    ///
    /// # Errors
    ///
    /// Returns a host, truncation, cleanup, cancellation, timeout, or transport
    /// failure. Missing host status is classified as a disconnect.
    pub fn finish(
        self,
        host_exit: Option<i32>,
    ) -> Result<WindowsHostCompletion, WindowsHostClientFailure> {
        if self.failed {
            return Err(protocol_malformed());
        }
        let Some(host_exit) = host_exit else {
            return Err(failure(
                WindowsHostClientFailureKind::HostDisconnected,
                "Windows process host disconnected before status collection",
            ));
        };
        let terminal = match self.terminal {
            Some(terminal) => terminal,
            None if host_exit != 0 => {
                return Err(failure(
                    WindowsHostClientFailureKind::HostFailed,
                    "Windows process host rejected or failed the request before settlement",
                ));
            }
            None => {
                return Err(failure(
                    WindowsHostClientFailureKind::ProtocolTruncated,
                    "Windows process host exited without its terminal event",
                ));
            }
        };
        if !terminal.cleanup_verified || terminal.active_processes_after_cleanup != 0 {
            return Err(failure(
                WindowsHostClientFailureKind::CleanupUnverified,
                "Windows process host did not prove its Job empty",
            ));
        }
        if host_exit != 0 {
            return Err(failure(
                WindowsHostClientFailureKind::HostFailed,
                "Windows process host exited unsuccessfully",
            ));
        }
        match terminal.termination {
            WindowsHostTermination::Natural => Ok(terminal),
            WindowsHostTermination::Cancelled => Err(failure(
                WindowsHostClientFailureKind::Cancelled,
                "Windows process host cancelled the child Job",
            )),
            WindowsHostTermination::Timeout => Err(failure(
                WindowsHostClientFailureKind::TimedOut,
                "Windows process host timed out the child Job",
            )),
            WindowsHostTermination::OutputLimit => {
                let kind = if terminal.stdout_bytes
                    > u64::try_from(self.limits.max_stdout_bytes).unwrap_or(u64::MAX)
                {
                    WindowsHostClientFailureKind::StdoutLimit
                } else if terminal.stderr_bytes
                    > u64::try_from(self.limits.max_stderr_bytes).unwrap_or(u64::MAX)
                {
                    WindowsHostClientFailureKind::StderrLimit
                } else {
                    WindowsHostClientFailureKind::HostFailed
                };
                Err(failure(
                    kind,
                    "Windows process host enforced an output limit",
                ))
            }
            WindowsHostTermination::ProtocolFailure => Err(protocol_malformed()),
            WindowsHostTermination::ParentDisconnected => Err(failure(
                WindowsHostClientFailureKind::HostDisconnected,
                "Windows process host observed its parent disconnect",
            )),
            WindowsHostTermination::IoFailure | WindowsHostTermination::Backpressure => {
                Err(failure(
                    WindowsHostClientFailureKind::HostFailed,
                    "Windows process host could not complete child I/O",
                ))
            }
        }
    }

    fn accept_line_inner(
        &mut self,
        line: &[u8],
    ) -> Result<Option<WindowsHostChunk>, WindowsHostClientFailure> {
        if self.terminal.is_some() {
            return Err(protocol_malformed());
        }
        let record = one_json_line(line, MAX_HOST_EVENT_BYTES)?;
        let object = exact_object(
            &record,
            &["schema", "requestId", "sequence", "type", "payload"],
        )?;
        require_string(object, "schema", EVENT_SCHEMA)?;
        require_string(object, "requestId", &self.request_id)?;
        if require_u64(object, "sequence", u64::MAX)? != self.next_sequence {
            return Err(protocol_malformed());
        }
        let event_type = string_field(object, "type", 64)?;
        let payload = object
            .get("payload")
            .and_then(Value::as_object)
            .ok_or_else(protocol_malformed)?;
        let output = match event_type {
            "host.started" => Some(self.accept_started(payload)?),
            "child.stdout" => Some(self.accept_stream(payload, true)?),
            "child.stderr" => Some(self.accept_stream(payload, false)?),
            "host.exited" => {
                self.accept_terminal(payload)?;
                None
            }
            _ => return Err(protocol_malformed()),
        };
        self.next_sequence = self
            .next_sequence
            .checked_add(1)
            .ok_or_else(protocol_malformed)?;
        Ok(output)
    }

    fn accept_started(
        &mut self,
        payload: &Map<String, Value>,
    ) -> Result<WindowsHostChunk, WindowsHostClientFailure> {
        if self.pid.is_some() {
            return Err(protocol_malformed());
        }
        exact_map_keys(
            payload,
            &[
                "pid",
                "suspendedCreate",
                "strictHandleList",
                "inheritedHandleCount",
                "jobAssignedBeforeResume",
                "killOnJobClose",
                "newProcessGroup",
                "outerPty",
                "shell",
            ],
        )?;
        let pid = require_u32(payload, "pid")?;
        if pid == 0
            || require_bool(payload, "suspendedCreate")? != Some(true)
            || require_bool(payload, "strictHandleList")? != Some(true)
            || require_u64(payload, "inheritedHandleCount", 3)? != 3
            || require_bool(payload, "jobAssignedBeforeResume")? != Some(true)
            || require_bool(payload, "killOnJobClose")? != Some(true)
            || require_bool(payload, "newProcessGroup")? != Some(true)
            || require_bool(payload, "outerPty")? != Some(false)
            || require_bool(payload, "shell")? != Some(false)
        {
            return Err(protocol_malformed());
        }
        self.pid = Some(pid);
        Ok(WindowsHostChunk::Started { pid })
    }

    fn accept_stream(
        &mut self,
        payload: &Map<String, Value>,
        stdout: bool,
    ) -> Result<WindowsHostChunk, WindowsHostClientFailure> {
        if self.pid.is_none() {
            return Err(protocol_malformed());
        }
        exact_map_keys(payload, &["encoding", "data", "stream"])?;
        require_string(payload, "encoding", "base64")?;
        require_string(payload, "stream", if stdout { "stdout" } else { "stderr" })?;
        let encoded = string_field(payload, "data", usize::MAX)?;
        let decoded = decode_base64_strict(encoded)?;
        let (observed, maximum, kind) = if stdout {
            (
                &mut self.stdout_bytes,
                self.limits.max_stdout_bytes,
                WindowsHostClientFailureKind::StdoutLimit,
            )
        } else {
            (
                &mut self.stderr_bytes,
                self.limits.max_stderr_bytes,
                WindowsHostClientFailureKind::StderrLimit,
            )
        };
        *observed = observed
            .checked_add(u64::try_from(decoded.len()).map_err(|_| protocol_malformed())?)
            .ok_or_else(protocol_malformed)?;
        if *observed > u64::try_from(maximum).unwrap_or(u64::MAX) {
            return Err(failure(
                kind,
                "decoded Windows host stream exceeded its bound",
            ));
        }
        Ok(if stdout {
            WindowsHostChunk::Stdout(decoded)
        } else {
            WindowsHostChunk::Stderr(decoded)
        })
    }

    #[allow(clippy::too_many_lines)]
    fn accept_terminal(
        &mut self,
        payload: &Map<String, Value>,
    ) -> Result<(), WindowsHostClientFailure> {
        let Some(started_pid) = self.pid else {
            return Err(protocol_malformed());
        };
        exact_map_keys(
            payload,
            &[
                "pid",
                "processExitCode",
                "termination",
                "gracefulControlAttempted",
                "gracefulControlDelivered",
                "hardKillUsed",
                "activeProcessesBeforeCleanup",
                "activeProcessesAfterCleanup",
                "cleanupVerified",
                "stdoutBytes",
                "stderrBytes",
            ],
        )?;
        let pid = require_u32(payload, "pid")?;
        if pid != started_pid {
            return Err(protocol_malformed());
        }
        let termination = match string_field(payload, "termination", 32)? {
            "natural" => WindowsHostTermination::Natural,
            "cancelled" => WindowsHostTermination::Cancelled,
            "parent-disconnected" => WindowsHostTermination::ParentDisconnected,
            "timeout" => WindowsHostTermination::Timeout,
            "output-limit" => WindowsHostTermination::OutputLimit,
            "io-failure" => WindowsHostTermination::IoFailure,
            "protocol-failure" => WindowsHostTermination::ProtocolFailure,
            "backpressure" => WindowsHostTermination::Backpressure,
            _ => return Err(protocol_malformed()),
        };
        let stdout_bytes = require_u64(payload, "stdoutBytes", u64::MAX)?;
        let stderr_bytes = require_u64(payload, "stderrBytes", u64::MAX)?;
        let exact_counts = stdout_bytes == self.stdout_bytes && stderr_bytes == self.stderr_bytes;
        let valid_output_limit_counts = termination == WindowsHostTermination::OutputLimit
            && ((stdout_bytes > u64::try_from(self.limits.max_stdout_bytes).unwrap_or(u64::MAX)
                && stderr_bytes == self.stderr_bytes)
                || (stderr_bytes
                    > u64::try_from(self.limits.max_stderr_bytes).unwrap_or(u64::MAX)
                    && stdout_bytes == self.stdout_bytes));
        if !exact_counts && !valid_output_limit_counts {
            return Err(protocol_malformed());
        }
        self.terminal = Some(WindowsHostCompletion {
            pid,
            process_exit_code: require_u32(payload, "processExitCode")?,
            termination,
            graceful_control_attempted: bool_field(payload, "gracefulControlAttempted")?,
            graceful_control_delivered: bool_field(payload, "gracefulControlDelivered")?,
            hard_kill_used: bool_field(payload, "hardKillUsed")?,
            active_processes_before_cleanup: require_u32(payload, "activeProcessesBeforeCleanup")?,
            active_processes_after_cleanup: require_u32(payload, "activeProcessesAfterCleanup")?,
            cleanup_verified: bool_field(payload, "cleanupVerified")?,
            stdout_bytes,
            stderr_bytes,
        });
        Ok(())
    }
}

/// Encodes one closed request JSONL record for the Windows process host.
///
/// # Errors
///
/// Returns a stable request or size failure before any helper can be spawned.
#[allow(clippy::too_many_lines)]
pub fn encode_windows_host_request(
    request: &WindowsHostRequest,
) -> Result<Vec<u8>, WindowsHostClientFailure> {
    validate_identifier(&request.request_id).map_err(|()| request_invalid())?;
    validate_path(&request.executable, true).map_err(|()| request_invalid())?;
    validate_path(&request.wrapper_executable, true).map_err(|()| request_invalid())?;
    validate_path(&request.cwd, false).map_err(|()| request_invalid())?;
    if request
        .executable
        .eq_ignore_ascii_case(&request.wrapper_executable)
    {
        return Err(request_invalid());
    }
    if request.argv.len() > MAX_ARGV_ITEMS
        || request.argv.iter().any(|argument| {
            argument.contains('\0') || argument.chars().count() > MAX_ARGUMENT_CHARS
        })
    {
        return Err(request_invalid());
    }
    validate_command_line(&request.executable, &request.argv).map_err(|()| request_limit())?;
    if request.environment.len() > MAX_ENVIRONMENT_ITEMS {
        return Err(request_limit());
    }
    let mut names = BTreeSet::new();
    let mut environment_utf16_bytes = 2_usize;
    for entry in &request.environment {
        if !valid_environment_name(&entry.name)
            || entry.name.chars().count() > MAX_ENVIRONMENT_NAME_CHARS
            || entry.value.contains('\0')
            || entry.value.chars().count() > MAX_ENVIRONMENT_VALUE_CHARS
            || !names.insert(entry.name.to_ascii_uppercase())
        {
            return Err(request_invalid());
        }
        environment_utf16_bytes = environment_utf16_bytes
            .checked_add(
                (entry.name.encode_utf16().count() + 1 + entry.value.encode_utf16().count() + 1)
                    .saturating_mul(2),
            )
            .ok_or_else(request_limit)?;
    }
    if environment_utf16_bytes > MAX_ENVIRONMENT_BYTES {
        return Err(request_limit());
    }
    if REQUIRED_METADATA.iter().any(|required| {
        !request
            .environment
            .iter()
            .any(|entry| entry.name.eq_ignore_ascii_case(required) && !entry.value.is_empty())
    }) {
        return Err(request_invalid());
    }
    if request.child_stdin.len() > MAX_CHILD_STDIN_BYTES {
        return Err(request_limit());
    }
    validate_limits(request.limits).map_err(|()| request_invalid())?;
    if request.cancellation.grace_ms > MAX_CANCELLATION_MS
        || request.cancellation.hard_kill_after_ms == 0
        || request.cancellation.hard_kill_after_ms > MAX_CANCELLATION_MS
        || request.run_timeout_ms == 0
        || request.run_timeout_ms > MAX_RUN_TIMEOUT_MS
    {
        return Err(request_invalid());
    }
    let environment = request
        .environment
        .iter()
        .map(|entry| json!({"name":entry.name, "value":entry.value}))
        .collect::<Vec<_>>();
    let value = json!({
        "schema": REQUEST_SCHEMA,
        "requestId": request.request_id,
        "executable": request.executable,
        "wrapperExecutable": request.wrapper_executable,
        "argv": request.argv,
        "cwd": request.cwd,
        "environment": environment,
        "stdinBase64": encode_base64(&request.child_stdin),
        "limits": {
            "maxStdoutBytes": request.limits.max_stdout_bytes,
            "maxStderrBytes": request.limits.max_stderr_bytes,
            "maxQueuedChunks": request.limits.max_queued_chunks,
        },
        "cancellation": {
            "graceful": request.cancellation.graceful.label(),
            "graceMs": request.cancellation.grace_ms,
            "hardKillAfterMs": request.cancellation.hard_kill_after_ms,
        },
        "runTimeoutMs": request.run_timeout_ms,
    });
    encode_line(&value, MAX_REQUEST_BYTES)
}

/// Encodes one closed cancellation control JSONL record.
///
/// # Errors
///
/// Returns a request error if the reason is outside the protocol identifier form.
pub fn encode_windows_cancel_control(reason: &str) -> Result<Vec<u8>, WindowsHostClientFailure> {
    validate_identifier(reason).map_err(|()| request_invalid())?;
    encode_line(
        &json!({"type":"cancel", "schema":CONTROL_SCHEMA, "reason":reason}),
        4096,
    )
}

/// Parses one bounded, closed bootstrap diagnostic from host stderr.
///
/// # Errors
///
/// Returns a distinct limit or malformed diagnostic classification. The input
/// is never echoed into the error message.
pub fn parse_windows_host_bootstrap_error(
    bytes: &[u8],
    maximum: usize,
) -> Result<WindowsHostBootstrapError, WindowsHostClientFailure> {
    if bytes.len() > maximum {
        return Err(failure(
            WindowsHostClientFailureKind::HostDiagnosticLimit,
            "Windows process host diagnostic exceeded its bound",
        ));
    }
    let record = one_json_line(bytes, maximum).map_err(|_| {
        failure(
            WindowsHostClientFailureKind::HostDiagnosticMalformed,
            "Windows process host diagnostic was malformed",
        )
    })?;
    let object = exact_object(&record, &["schema", "code", "operation", "win32"])
        .map_err(|_| host_diagnostic_malformed())?;
    require_string(object, "schema", ERROR_SCHEMA).map_err(|_| host_diagnostic_malformed())?;
    let code = string_field(object, "code", 128).map_err(|_| host_diagnostic_malformed())?;
    let operation =
        string_field(object, "operation", 256).map_err(|_| host_diagnostic_malformed())?;
    if code.chars().any(char::is_control)
        || operation.chars().any(char::is_control)
        || code.is_empty()
        || operation.is_empty()
    {
        return Err(host_diagnostic_malformed());
    }
    let win32 = match object.get("win32") {
        Some(Value::Null) => None,
        Some(value) => Some(
            value
                .as_u64()
                .and_then(|number| u32::try_from(number).ok())
                .ok_or_else(host_diagnostic_malformed)?,
        ),
        None => return Err(host_diagnostic_malformed()),
    };
    Ok(WindowsHostBootstrapError {
        code: code.to_owned(),
        operation: operation.to_owned(),
        win32,
    })
}

/// Validates the authenticated helper's closed protocol identity report.
///
/// The report establishes protocol applicability only. Its claims never
/// enable admission, which is exclusively a compiled runner decision.
///
/// # Errors
///
/// Returns a protocol failure for an oversized, open, malformed, non-Windows,
/// or incompatible identity.
#[allow(clippy::too_many_lines)]
pub fn validate_windows_host_identity(
    bytes: &[u8],
    maximum: usize,
) -> Result<WindowsHostIdentity, WindowsHostClientFailure> {
    let value = one_json_line(bytes, maximum)?;
    let root = exact_object(
        &value,
        &[
            "schema",
            "component",
            "componentVersion",
            "target",
            "protocol",
            "claims",
        ],
    )?;
    require_string(root, "schema", IDENTITY_SCHEMA)?;
    require_string(root, "component", "openprose-windows-process-host")?;
    let component_version = string_field(root, "componentVersion", 128)?;
    if !valid_semver_identity(component_version) {
        return Err(protocol_malformed());
    }
    let target = exact_object(
        root.get("target").ok_or_else(protocol_malformed)?,
        &["os", "architecture", "nativeWindowsImplementation"],
    )?;
    require_string(target, "os", "windows")?;
    let architecture = string_field(target, "architecture", 64)?;
    if architecture.is_empty()
        || !architecture
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        || !bool_field(target, "nativeWindowsImplementation")?
    {
        return Err(protocol_malformed());
    }
    let protocol = exact_object(
        root.get("protocol").ok_or_else(protocol_malformed)?,
        &[
            "request",
            "control",
            "event",
            "error",
            "maxRequestBytes",
            "maxControlBytes",
            "limits",
        ],
    )?;
    require_string(protocol, "request", REQUEST_SCHEMA)?;
    require_string(protocol, "control", CONTROL_SCHEMA)?;
    require_string(protocol, "event", EVENT_SCHEMA)?;
    require_string(protocol, "error", ERROR_SCHEMA)?;
    require_exact_u64(protocol, "maxRequestBytes", MAX_REQUEST_BYTES as u64)?;
    require_exact_u64(protocol, "maxControlBytes", 4096)?;
    let limits = exact_object(
        protocol.get("limits").ok_or_else(protocol_malformed)?,
        &[
            "maxDecodedStdinBytes",
            "maxStdinBase64Characters",
            "maxArgvItems",
            "maxArgumentCharacters",
            "maxEnvironmentItems",
            "maxEnvironmentNameCharacters",
            "maxEnvironmentValueCharacters",
            "maxPathCharacters",
            "maxEnvironmentBlockBytes",
            "maxRenderedCommandLineUtf16Units",
        ],
    )?;
    for (field, expected) in [
        ("maxDecodedStdinBytes", MAX_CHILD_STDIN_BYTES as u64),
        ("maxStdinBase64Characters", 22_369_620),
        ("maxArgvItems", MAX_ARGV_ITEMS as u64),
        ("maxArgumentCharacters", MAX_ARGUMENT_CHARS as u64),
        ("maxEnvironmentItems", MAX_ENVIRONMENT_ITEMS as u64),
        (
            "maxEnvironmentNameCharacters",
            MAX_ENVIRONMENT_NAME_CHARS as u64,
        ),
        (
            "maxEnvironmentValueCharacters",
            MAX_ENVIRONMENT_VALUE_CHARS as u64,
        ),
        ("maxPathCharacters", MAX_PATH_CHARS as u64),
        ("maxEnvironmentBlockBytes", MAX_ENVIRONMENT_BYTES as u64),
        (
            "maxRenderedCommandLineUtf16Units",
            MAX_COMMAND_LINE_UNITS as u64,
        ),
    ] {
        require_exact_u64(limits, field, expected)?;
    }
    let claims = exact_object(
        root.get("claims").ok_or_else(protocol_malformed)?,
        &[
            "providerCallsMade",
            "nativeWindowsRuntimeEvidence",
            "strictWindowsContainmentReady",
        ],
    )?;
    if bool_field(claims, "providerCallsMade")?
        || bool_field(claims, "nativeWindowsRuntimeEvidence")?
        || bool_field(claims, "strictWindowsContainmentReady")?
    {
        return Err(protocol_malformed());
    }
    Ok(WindowsHostIdentity {
        component_version: component_version.to_owned(),
        architecture: architecture.to_owned(),
    })
}

/// Validates bounded UTF-8 output from a naturally completed version probe.
///
/// Version-range policy remains adapter-owned and is intentionally not applied here.
///
/// # Errors
///
/// Returns `VersionProbeInvalid` for a nonzero child exit, empty response,
/// invalid UTF-8, embedded NUL, or output beyond the caller's bound.
pub fn validate_windows_version_probe(
    completion: &WindowsHostCompletion,
    stdout: &[u8],
    maximum: usize,
) -> Result<String, WindowsHostClientFailure> {
    if completion.termination != WindowsHostTermination::Natural
        || completion.process_exit_code != 0
        || stdout.is_empty()
        || stdout.len() > maximum
        || stdout.contains(&0)
    {
        return Err(version_probe_invalid());
    }
    let version = std::str::from_utf8(stdout)
        .map_err(|_| version_probe_invalid())?
        .trim();
    if version.is_empty() {
        return Err(version_probe_invalid());
    }
    Ok(version.to_owned())
}

fn validate_limits(limits: WindowsHostLimits) -> Result<(), ()> {
    if !(1..=268_435_456).contains(&limits.max_stdout_bytes)
        || !(1..=MAX_STREAM_BYTES).contains(&limits.max_stderr_bytes)
        || !(1..=MAX_QUEUED_CHUNKS).contains(&limits.max_queued_chunks)
    {
        return Err(());
    }
    Ok(())
}

fn validate_identifier(value: &str) -> Result<(), ()> {
    if value.is_empty()
        || value.len() > MAX_IDENTIFIER_BYTES
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(());
    }
    Ok(())
}

fn validate_path(value: &str, executable: bool) -> Result<(), ()> {
    if value.contains('\0') || value.chars().count() > MAX_PATH_CHARS {
        return Err(());
    }
    let bytes = value.as_bytes();
    let drive = bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/');
    let unc = value.starts_with("\\\\")
        && value[2..]
            .split(['\\', '/'])
            .filter(|part| !part.is_empty())
            .count()
            >= 2;
    if (!drive && !unc) || (executable && !value.to_ascii_lowercase().ends_with(".exe")) {
        return Err(());
    }
    Ok(())
}

fn validate_command_line(executable: &str, argv: &[String]) -> Result<(), ()> {
    let mut rendered = quote_windows_argument(executable)?;
    for argument in argv {
        rendered.push(' ');
        rendered.push_str(&quote_windows_argument(argument)?);
    }
    if rendered.encode_utf16().count() > MAX_COMMAND_LINE_UNITS {
        return Err(());
    }
    Ok(())
}

fn quote_windows_argument(argument: &str) -> Result<String, ()> {
    if argument.contains('\0') {
        return Err(());
    }
    if !argument.is_empty()
        && !argument
            .chars()
            .any(|character| character.is_whitespace() || character == '"')
    {
        return Ok(argument.to_owned());
    }
    let mut quoted = String::from("\"");
    let mut backslashes = 0_usize;
    for character in argument.chars() {
        if character == '\\' {
            backslashes += 1;
            continue;
        }
        if character == '"' {
            quoted.extend(std::iter::repeat_n('\\', backslashes * 2 + 1));
            quoted.push('"');
        } else {
            quoted.extend(std::iter::repeat_n('\\', backslashes));
            quoted.push(character);
        }
        backslashes = 0;
    }
    quoted.extend(std::iter::repeat_n('\\', backslashes * 2));
    quoted.push('"');
    Ok(quoted)
}

fn valid_environment_name(value: &str) -> bool {
    !value.is_empty()
        && value.is_ascii()
        && !value.as_bytes()[0].is_ascii_digit()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

fn encode_line(value: &Value, maximum: usize) -> Result<Vec<u8>, WindowsHostClientFailure> {
    let mut encoded = serde_json::to_vec(value).map_err(|_| request_invalid())?;
    encoded.push(b'\n');
    if encoded.len() > maximum {
        return Err(request_limit());
    }
    Ok(encoded)
}

fn one_json_line(bytes: &[u8], maximum: usize) -> Result<Value, WindowsHostClientFailure> {
    if bytes.is_empty() || bytes.len() > maximum {
        return Err(protocol_malformed());
    }
    let mut line = bytes;
    if line.last() == Some(&b'\n') {
        line = &line[..line.len() - 1];
        if line.last() == Some(&b'\r') {
            line = &line[..line.len() - 1];
        }
    }
    if line.is_empty() || line.contains(&b'\n') || line.contains(&b'\r') {
        return Err(protocol_malformed());
    }
    serde_json::from_slice(line).map_err(|_| protocol_malformed())
}

fn exact_object<'a>(
    value: &'a Value,
    keys: &[&str],
) -> Result<&'a Map<String, Value>, WindowsHostClientFailure> {
    let object = value.as_object().ok_or_else(protocol_malformed)?;
    exact_map_keys(object, keys)?;
    Ok(object)
}

fn exact_map_keys(
    object: &Map<String, Value>,
    keys: &[&str],
) -> Result<(), WindowsHostClientFailure> {
    if object.len() != keys.len() || keys.iter().any(|key| !object.contains_key(*key)) {
        return Err(protocol_malformed());
    }
    Ok(())
}

fn string_field<'a>(
    object: &'a Map<String, Value>,
    field: &str,
    maximum: usize,
) -> Result<&'a str, WindowsHostClientFailure> {
    let value = object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(protocol_malformed)?;
    if value.len() > maximum {
        return Err(protocol_malformed());
    }
    Ok(value)
}

fn require_string(
    object: &Map<String, Value>,
    field: &str,
    expected: &str,
) -> Result<(), WindowsHostClientFailure> {
    if string_field(object, field, expected.len())? != expected {
        return Err(protocol_malformed());
    }
    Ok(())
}

fn require_u64(
    object: &Map<String, Value>,
    field: &str,
    maximum: u64,
) -> Result<u64, WindowsHostClientFailure> {
    let value = object
        .get(field)
        .and_then(Value::as_u64)
        .filter(|value| *value <= maximum)
        .ok_or_else(protocol_malformed)?;
    Ok(value)
}

fn require_u32(object: &Map<String, Value>, field: &str) -> Result<u32, WindowsHostClientFailure> {
    u32::try_from(require_u64(object, field, u64::from(u32::MAX))?)
        .map_err(|_| protocol_malformed())
}

fn require_exact_u64(
    object: &Map<String, Value>,
    field: &str,
    expected: u64,
) -> Result<(), WindowsHostClientFailure> {
    if require_u64(object, field, u64::MAX)? != expected {
        return Err(protocol_malformed());
    }
    Ok(())
}

fn valid_semver_identity(value: &str) -> bool {
    let marker = value.find(['-', '+']);
    let core = marker.map_or(value, |index| &value[..index]);
    if marker.is_some_and(|index| {
        let suffix = &value[index + 1..];
        suffix.is_empty()
            || !suffix
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
    }) {
        return false;
    }
    let mut parts = core.split('.');
    let valid_number = |part: &str| {
        !part.is_empty()
            && part.bytes().all(|byte| byte.is_ascii_digit())
            && (part == "0" || !part.starts_with('0'))
    };
    valid_number(parts.next().unwrap_or_default())
        && valid_number(parts.next().unwrap_or_default())
        && valid_number(parts.next().unwrap_or_default())
        && parts.next().is_none()
}

fn require_bool(
    object: &Map<String, Value>,
    field: &str,
) -> Result<Option<bool>, WindowsHostClientFailure> {
    object
        .get(field)
        .map(Value::as_bool)
        .ok_or_else(protocol_malformed)
}

fn bool_field(object: &Map<String, Value>, field: &str) -> Result<bool, WindowsHostClientFailure> {
    require_bool(object, field)?.ok_or_else(protocol_malformed)
}

fn encode_base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let first = chunk[0];
        let second = *chunk.get(1).unwrap_or(&0);
        let third = *chunk.get(2).unwrap_or(&0);
        output.push(char::from(ALPHABET[usize::from(first >> 2)]));
        output.push(char::from(
            ALPHABET[usize::from((first & 0x03) << 4 | second >> 4)],
        ));
        output.push(if chunk.len() > 1 {
            char::from(ALPHABET[usize::from((second & 0x0f) << 2 | third >> 6)])
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            char::from(ALPHABET[usize::from(third & 0x3f)])
        } else {
            '='
        });
    }
    output
}

fn decode_base64_strict(value: &str) -> Result<Vec<u8>, WindowsHostClientFailure> {
    if value.len() % 4 != 0 || !value.is_ascii() {
        return Err(protocol_malformed());
    }
    let mut output = Vec::with_capacity(value.len() / 4 * 3);
    let chunks = value.as_bytes().chunks_exact(4);
    let chunk_count = chunks.len();
    for (index, chunk) in chunks.enumerate() {
        let last = index + 1 == chunk_count;
        let first = base64_value(chunk[0]).ok_or_else(protocol_malformed)?;
        let second = base64_value(chunk[1]).ok_or_else(protocol_malformed)?;
        output.push(first << 2 | second >> 4);
        if chunk[2] == b'=' {
            if !last || chunk[3] != b'=' || second & 0x0f != 0 {
                return Err(protocol_malformed());
            }
            continue;
        }
        let third = base64_value(chunk[2]).ok_or_else(protocol_malformed)?;
        output.push(second << 4 | third >> 2);
        if chunk[3] == b'=' {
            if !last || third & 0x03 != 0 {
                return Err(protocol_malformed());
            }
            continue;
        }
        let fourth = base64_value(chunk[3]).ok_or_else(protocol_malformed)?;
        output.push(third << 6 | fourth);
    }
    Ok(output)
}

const fn base64_value(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

const fn failure(
    kind: WindowsHostClientFailureKind,
    message: &'static str,
) -> WindowsHostClientFailure {
    WindowsHostClientFailure::new(kind, message)
}

const fn request_invalid() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::RequestInvalid,
        "Windows process host request is outside the closed contract",
    )
}

const fn request_limit() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::RequestLimit,
        "Windows process host request exceeded a fixed bound",
    )
}

const fn protocol_malformed() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::ProtocolMalformed,
        "Windows process host emitted a malformed event",
    )
}

const fn host_diagnostic_malformed() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::HostDiagnosticMalformed,
        "Windows process host diagnostic was malformed",
    )
}

const fn version_probe_invalid() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::VersionProbeInvalid,
        "Windows process version probe returned an invalid response",
    )
}

const fn host_unavailable() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::HostUnavailable,
        "compiled Windows process host is unavailable",
    )
}

const fn host_integrity() -> WindowsHostClientFailure {
    failure(
        WindowsHostClientFailureKind::HostIntegrity,
        "compiled Windows process host failed integrity verification",
    )
}

#[test]
fn output_budget_keeps_windows_stderr_boundary() {
    let mut limits = WindowsHostLimits {
        max_stdout_bytes: 268_435_456,
        max_stderr_bytes: 67_108_864,
        max_queued_chunks: 1,
    };
    assert!(validate_limits(limits).is_ok());
    limits.max_stdout_bytes += 1;
    assert!(validate_limits(limits).is_err());
    limits.max_stdout_bytes = 268_435_456;
    limits.max_stderr_bytes += 1;
    assert!(validate_limits(limits).is_err());
}
