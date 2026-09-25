#![allow(clippy::result_large_err)]

use crate::environment::{EnvironmentPolicy, Sensitivity};
use crate::framing::{ReaderMessage, StreamLimits, read_jsonl, read_stderr};
use crate::platform::{self, ContainmentClaim};
use crate::{INVOCATION_ID, RECURSION_MARKER, RUN_NONCE};
use serde_json::Value;
use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fmt::{self, Debug, Formatter};
use std::fs;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, TrySendError, sync_channel};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const PROBE_READER_POLL_INTERVAL: Duration = Duration::from_millis(5);
const PROBE_READER_STOP_TIMEOUT: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureKind {
    HarnessUnavailable,
    HarnessIncompatible,
    RecursiveInvocation,
    StartupTimeout,
    RunTimeout,
    ProtocolMalformed,
    ProtocolTruncated,
    HarnessFailed,
    Cancelled,
    CleanupFailed,
    ContainmentUnsupported,
    Internal,
}

#[derive(Debug, Clone)]
pub struct SupervisorFailure {
    pub kind: FailureKind,
    pub message: String,
    pub stderr: String,
    pub process_exit: Option<i32>,
    pub process_signal: Option<String>,
    pub process_started: bool,
    pub terminal_observed: bool,
    pub terminal_envelope: Option<Value>,
    pub records: Vec<Value>,
    pub transport_diagnostic: Option<Value>,
}

impl SupervisorFailure {
    pub(crate) fn new(kind: FailureKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            stderr: String::new(),
            process_exit: None,
            process_signal: None,
            process_started: false,
            terminal_observed: false,
            terminal_envelope: None,
            records: Vec::new(),
            transport_diagnostic: None,
        }
    }

    #[must_use]
    pub fn transport_diagnostic(&self) -> Option<Value> {
        if let Some(value) = &self.transport_diagnostic {
            return Some(value.clone());
        }
        if !matches!(
            self.kind,
            FailureKind::ProtocolMalformed | FailureKind::ProtocolTruncated
        ) {
            return None;
        }
        let reason = match self.message.as_str() {
            "harness emitted a malformed JSONL record" => "invalid-json",
            "harness emitted a non-object JSONL record" => "non-object-record",
            "harness emitted an empty structured record" => "empty-record",
            "harness structured output exceeded a fixed record limit" => "record-byte-limit",
            "harness structured output exceeded a fixed transport limit" => {
                "aggregate-stdout-limit"
            }
            "harness stream ended in the middle of a structured record" => "truncated-record",
            _ => "lifecycle-rejection",
        };
        Some(serde_json::json!({"schema":"openprose.transport-diagnostic/1","reason":reason}))
    }

    fn with_observed_diagnostic(mut self, reason: &str, observed: usize) -> Self {
        let mut diagnostic = serde_json::json!({
            "schema": "openprose.transport-diagnostic/1",
            "reason": reason,
            "observedBytes": observed.min(u32::MAX as usize),
        });
        if observed > u32::MAX as usize {
            diagnostic["saturated"] = Value::Bool(true);
        }
        self.transport_diagnostic = Some(diagnostic);
        self
    }

    pub(crate) fn with_byte_diagnostic(
        mut self,
        record: bool,
        observed: usize,
        limit: usize,
    ) -> Self {
        self.transport_diagnostic = Some(
            serde_json::json!({"schema":"openprose.transport-diagnostic/1","reason":if record {"record-byte-limit"} else {"aggregate-stdout-limit"},"observedBytes":observed.min(u32::MAX as usize),"limitBytes":limit.min(u32::MAX as usize),"saturated":observed > u32::MAX as usize || limit > u32::MAX as usize}),
        );
        self
    }

    #[must_use]
    pub fn protocol_failure_is_framing(&self) -> bool {
        match self.kind {
            FailureKind::ProtocolMalformed => matches!(
                self.message.as_str(),
                "harness emitted a malformed JSONL record"
                    | "harness emitted invalid UTF-8 structured output"
                    | "harness emitted a non-object JSONL record"
                    | "harness emitted an empty structured record"
                    | "harness structured output exceeded a fixed record limit"
                    | "harness structured output exceeded a fixed transport limit"
            ),
            FailureKind::ProtocolTruncated => matches!(
                self.message.as_str(),
                "harness stream ended in the middle of a structured record"
                    | "harness structured output could not be read to completion"
            ),
            _ => false,
        }
    }
}

impl std::error::Error for SupervisorFailure {}

impl fmt::Display for SupervisorFailure {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

#[derive(Debug, Clone, Default)]
pub struct CancellationToken(Arc<AtomicBool>);

impl CancellationToken {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }

    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

/// Keeps catchable operating-system cancellation handlers registered for a run.
///
/// POSIX and Windows `SIGINT`/`SIGTERM` set the same cooperative token used by
/// the supervisor. Other platforms retain a no-op guard.
#[must_use]
#[derive(Debug)]
pub struct SignalCancellationGuard {
    #[cfg(any(unix, windows))]
    registrations: Vec<signal_hook::SigId>,
}

impl SignalCancellationGuard {
    /// Registers catchable wrapper cancellation without doing work in a signal handler.
    ///
    /// # Errors
    ///
    /// Returns an operating-system error if a POSIX handler cannot be installed.
    pub fn install(token: &CancellationToken) -> std::io::Result<Self> {
        #[cfg(any(unix, windows))]
        {
            use signal_hook::consts::{SIGINT, SIGTERM};

            let mut registrations = Vec::with_capacity(2);
            for signal in [SIGINT, SIGTERM] {
                match signal_hook::flag::register(signal, Arc::clone(&token.0)) {
                    Ok(registration) => registrations.push(registration),
                    Err(error) => {
                        for registration in registrations {
                            signal_hook::low_level::unregister(registration);
                        }
                        return Err(error);
                    }
                }
            }
            Ok(Self { registrations })
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = token;
            Ok(Self {})
        }
    }
}

impl Drop for SignalCancellationGuard {
    fn drop(&mut self) {
        #[cfg(any(unix, windows))]
        for registration in self.registrations.drain(..) {
            signal_hook::low_level::unregister(registration);
        }
    }
}

#[derive(Clone)]
pub struct ProcessSpec {
    pub executable: PathBuf,
    pub argv: Vec<OsString>,
    pub cwd: PathBuf,
    pub wrapper_executable: Option<PathBuf>,
    pub version_probe: Option<VersionProbe>,
    /// Exact adapter input bytes. `None` starts with stdin closed; `Some`
    /// writes the bytes once through a pipe. The bytes are omitted from
    /// `Debug` and diagnostics.
    pub stdin: Option<Vec<u8>>,
    /// Controls whether the write end closes immediately after delivery or is
    /// retained until the structured protocol terminal is accepted. RPC
    /// harnesses that drain accepted work on EOF require the latter.
    pub stdin_lifecycle: StdinLifecycle,
    pub environment: EnvironmentPolicy,
    pub invocation_id: String,
    pub recursion_token: String,
    pub run_nonce: String,
    pub startup_timeout: Duration,
    pub run_timeout: Duration,
    pub termination_grace: Duration,
    pub limits: StreamLimits,
    pub cancellation: CancellationToken,
    /// Deterministic conformance control measured from the accepted startup
    /// record. Production callers leave this unset and use `cancellation`.
    pub cancel_after_start: Option<Duration>,
}

impl Debug for ProcessSpec {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ProcessSpec")
            .field("executable", &self.executable)
            .field("argv", &self.argv)
            .field("cwd", &self.cwd)
            .field("wrapper_executable", &self.wrapper_executable)
            .field("version_probe", &self.version_probe)
            .field("stdin_lifecycle", &self.stdin_lifecycle)
            .field("environment", &self.environment)
            .field("invocation_id", &self.invocation_id)
            .field("recursion_token", &"[REDACTED]")
            .field("run_nonce", &"[REDACTED]")
            .field("startup_timeout", &self.startup_timeout)
            .field("run_timeout", &self.run_timeout)
            .field("termination_grace", &self.termination_grace)
            .field("limits", &self.limits)
            .field("cancel_after_start", &self.cancel_after_start)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StdinLifecycle {
    CloseAfterWrite,
    CloseAfterTerminalEvent,
}

#[derive(Debug, Clone)]
pub struct VersionProbe {
    pub argv: Vec<OsString>,
    pub timeout: Duration,
    pub max_output_bytes: usize,
    pub required_substring: Option<String>,
    pub output: VersionProbeOutput,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VersionProbeOutput {
    Stdout,
    Stderr,
}

#[derive(Debug, Clone)]
pub struct CommandProbe {
    pub argv: Vec<OsString>,
    pub timeout: Duration,
    pub max_output_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandProbeOutcome {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Clone)]
pub struct JsonlProtocol {
    pub schema: Option<String>,
    pub start_event: String,
    pub terminal_event: String,
    pub allowed_events: BTreeSet<String>,
    pub failure_events: BTreeSet<String>,
    pub allowed_after_terminal_events: BTreeSet<String>,
    pub terminal_envelope_field: Option<String>,
    pub terminal_is_candidate: bool,
}

impl JsonlProtocol {
    #[must_use]
    pub fn fake_harness() -> Self {
        Self {
            schema: Some("openprose.fake-harness-event/1".to_owned()),
            start_event: "session.started".to_owned(),
            terminal_event: "session.completed".to_owned(),
            allowed_events: ["assistant.message", "fixture.descendants"]
                .into_iter()
                .map(ToOwned::to_owned)
                .collect(),
            failure_events: BTreeSet::new(),
            allowed_after_terminal_events: BTreeSet::new(),
            terminal_envelope_field: Some("terminalEnvelope".to_owned()),
            terminal_is_candidate: false,
        }
    }

    /// Describes an installed harness JSONL stream whose terminal record is
    /// retained whole for adapter-specific normalization.
    #[must_use]
    pub fn installed(
        start_event: impl Into<String>,
        terminal_event: impl Into<String>,
        allowed_events: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            schema: None,
            start_event: start_event.into(),
            terminal_event: terminal_event.into(),
            allowed_events: allowed_events.into_iter().map(Into::into).collect(),
            failure_events: BTreeSet::new(),
            allowed_after_terminal_events: BTreeSet::new(),
            terminal_envelope_field: None,
            terminal_is_candidate: false,
        }
    }

    /// Marks structured records which are explicit harness failures rather
    /// than malformed/unknown protocol events.
    #[must_use]
    pub fn with_failure_events(
        mut self,
        failure_events: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        self.failure_events = failure_events.into_iter().map(Into::into).collect();
        self
    }

    /// Permits only the listed non-event response records after the protocol's
    /// lifecycle terminal. This exists for RPC servers whose immediate command
    /// acknowledgement may be serialized after the completed agent lifecycle.
    #[must_use]
    pub fn with_allowed_after_terminal_events(
        mut self,
        events: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        self.allowed_after_terminal_events = events.into_iter().map(Into::into).collect();
        self
    }
}

#[derive(Debug, Clone)]
pub struct ProcessOutcome {
    pub executable: PathBuf,
    pub cwd: PathBuf,
    pub records: Vec<Value>,
    pub terminal_envelope: Value,
    pub stderr: String,
    pub process_exit: i32,
    pub process_signal: Option<String>,
    pub containment: ContainmentClaim,
    pub probed_version: Option<String>,
    pub duration: Duration,
}

/// Receives one structurally admitted harness record at a time.
///
/// Observers never see raw bytes, partial records, or stderr. The optional
/// pre-admission hook receives parsed JSON, including rejected records, on Unix. Adapter-specific callers must still
/// validate a record before deriving any user-visible projection from it.
pub trait RecordObserver {
    /// Evidence-only hook; receiving a value never admits it. Unix transport only.
    ///
    /// # Errors
    ///
    /// Returns a [`SupervisorFailure`] when the observer refuses the record.
    fn observe_parsed(&mut self, _record: &Value) -> Result<(), SupervisorFailure> {
        Ok(())
    }

    /// Observes a single admitted record.
    ///
    /// # Errors
    ///
    /// Returning an error terminates and settles the supervised process.
    fn observe(&mut self, record: &Value) -> Result<(), SupervisorFailure>;

    /// Returns one bounded controller-produced stdin frame after an admitted
    /// record. The default preserves ordinary read-only observers.
    ///
    /// # Errors
    ///
    /// Returning an error terminates and settles the supervised process.
    fn take_stdin_write(&mut self) -> Result<Option<Vec<u8>>, SupervisorFailure> {
        Ok(None)
    }

    /// Close staged stdin after its acknowledgement.
    fn close_stdin_requested(&self) -> bool {
        false
    }

    /// Replaces the just-observed record before the supervisor retains it.
    /// `None` preserves the admitted record byte-for-byte.
    fn retained_record_projection(&self, _record: &Value) -> Option<Value> {
        None
    }

    /// Declares that this observer controls staged stdin and therefore cannot
    /// use the current Windows host transport.
    fn requires_staged_stdin(&self) -> bool {
        false
    }
}

impl<F> RecordObserver for F
where
    F: FnMut(&Value) -> Result<(), SupervisorFailure>,
{
    fn observe(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
        self(record)
    }
}

#[derive(Debug, Default)]
pub(crate) struct ProtocolState {
    pub(crate) started: bool,
    pub(crate) terminal: Option<Value>,
    pub(crate) records: Vec<Value>,
}

impl ProtocolState {
    pub(crate) fn accept(
        &mut self,
        bytes: &[u8],
        protocol: &JsonlProtocol,
    ) -> Result<(), SupervisorFailure> {
        if std::str::from_utf8(bytes).is_err() {
            let mut error = SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted invalid UTF-8 structured output",
            );
            error.transport_diagnostic = Some(
                serde_json::json!({"schema":"openprose.transport-diagnostic/1","reason":"invalid-utf8","observedBytes":bytes.len().min(u32::MAX as usize)}),
            );
            return Err(error);
        }
        let value: Value = serde_json::from_slice(bytes).map_err(|_| {
            SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted a malformed JSONL record",
            )
            .with_observed_diagnostic("invalid-json", bytes.len())
        })?;
        let object = value.as_object().ok_or_else(|| {
            SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted a non-object JSONL record",
            )
            .with_observed_diagnostic("non-object-record", bytes.len())
        })?;
        if let Some(expected_schema) = protocol.schema.as_deref() {
            if object.get("schema").and_then(Value::as_str) != Some(expected_schema) {
                return Err(SupervisorFailure::new(
                    FailureKind::ProtocolMalformed,
                    "harness emitted a record with an unexpected schema",
                ));
            }
        }
        let event_type = object.get("type").and_then(Value::as_str).ok_or_else(|| {
            SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted a record without an event type",
            )
        })?;
        if self.terminal.is_some()
            && !protocol.terminal_is_candidate
            && !protocol.allowed_after_terminal_events.contains(event_type)
        {
            return Err(SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted a record after its terminal record",
            ));
        }
        if self.terminal.is_some() && !protocol.terminal_is_candidate {
            self.records.push(value);
            return Ok(());
        }
        if protocol.failure_events.contains(event_type) {
            return Err(SupervisorFailure::new(
                FailureKind::HarnessFailed,
                "harness reported a structured failure event",
            ));
        }
        if !self.started {
            if event_type != protocol.start_event {
                return Err(SupervisorFailure::new(
                    FailureKind::ProtocolMalformed,
                    "harness emitted an out-of-order record before session start",
                ));
            }
            self.started = true;
        } else if event_type == protocol.start_event
            && !protocol.allowed_events.contains(event_type)
        {
            return Err(SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted a duplicate session-start record",
            ));
        } else if event_type == protocol.terminal_event {
            let terminal = if let Some(field) = &protocol.terminal_envelope_field {
                object
                    .get(field)
                    .filter(|value| value.is_object())
                    .cloned()
                    .ok_or_else(|| {
                        SupervisorFailure::new(
                            FailureKind::ProtocolMalformed,
                            "harness terminal record omitted its structured terminal envelope",
                        )
                    })?
            } else {
                value.clone()
            };
            self.terminal = Some(terminal);
        } else if !protocol.allowed_events.contains(event_type) {
            return Err(SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "harness emitted an unsupported structured event type",
            ));
        }
        self.records.push(value);
        Ok(())
    }
}

#[derive(Debug, Default)]
#[cfg(any(windows, test))]
pub(crate) struct HarnessJsonlAssembler {
    record: Vec<u8>,
    maximum: usize,
}

#[cfg(any(windows, test))]
impl HarnessJsonlAssembler {
    pub(crate) fn new(maximum: usize) -> Self {
        Self {
            record: Vec::new(),
            maximum,
        }
    }

    pub(crate) fn push(
        &mut self,
        bytes: &[u8],
        state: &mut ProtocolState,
        protocol: &JsonlProtocol,
    ) -> Result<(), SupervisorFailure> {
        for byte in bytes {
            if *byte == b'\n' {
                if self.record.last() == Some(&b'\r') {
                    self.record.pop();
                }
                if self.record.is_empty() {
                    return Err(SupervisorFailure::new(
                        FailureKind::ProtocolMalformed,
                        "harness emitted an empty structured record",
                    ));
                }
                state.accept(&self.record, protocol)?;
                self.record.clear();
            } else {
                if self.record.len() >= self.maximum {
                    return Err(SupervisorFailure::new(
                        FailureKind::ProtocolMalformed,
                        "harness structured output exceeded a fixed record limit",
                    ));
                }
                self.record.push(*byte);
            }
        }
        Ok(())
    }

    pub(crate) fn finish(&self) -> Result<(), SupervisorFailure> {
        if self.record.is_empty() {
            Ok(())
        } else {
            Err(SupervisorFailure::new(
                FailureKind::ProtocolTruncated,
                "harness stream ended in the middle of a structured record",
            )
            .with_observed_diagnostic("truncated-record", self.record.len()))
        }
    }
}

/// Runs a process to one terminal decision while preserving stdout as a
/// bounded structured channel and stderr as a separately bounded diagnostic.
///
/// # Errors
///
/// Returns a closed mechanical failure for resolution, recursion, version
/// admission, spawn, containment, timeout, cancellation, protocol, process
/// exit, or cleanup failure.
#[allow(clippy::too_many_lines)]
pub fn supervise(
    spec: ProcessSpec,
    protocol: &JsonlProtocol,
) -> Result<ProcessOutcome, SupervisorFailure> {
    #[cfg(windows)]
    {
        crate::windows_supervision::supervise_windows(spec, protocol, None)
    }
    #[cfg(not(windows))]
    {
        supervise_direct(spec, protocol, None)
    }
}

/// Runs a process while exposing only structurally admitted records to an
/// adapter-specific observer.
///
/// This is intentionally separate from [`supervise`], so machine-output and
/// buffered callers retain byte-for-byte behavior without an observer.
///
/// # Errors
///
/// Returns the same closed failures as [`supervise`], plus an observer failure
/// after the process has been terminated and settled.
pub fn supervise_observed(
    spec: ProcessSpec,
    protocol: &JsonlProtocol,
    observer: &mut dyn RecordObserver,
) -> Result<ProcessOutcome, SupervisorFailure> {
    #[cfg(windows)]
    {
        if observer.requires_staged_stdin() {
            return Err(SupervisorFailure::new(
                FailureKind::ContainmentUnsupported,
                "staged adapter stdin is not supported by the Windows host transport",
            ));
        }
        crate::windows_supervision::supervise_windows(spec, protocol, Some(observer))
    }
    #[cfg(not(windows))]
    {
        supervise_direct(spec, protocol, Some(observer))
    }
}

#[cfg_attr(windows, allow(dead_code))]
#[allow(clippy::too_many_lines)]
fn supervise_direct(
    mut spec: ProcessSpec,
    protocol: &JsonlProtocol,
    mut observer: Option<&mut dyn RecordObserver>,
) -> Result<ProcessOutcome, SupervisorFailure> {
    if spec.cancellation.is_cancelled() {
        return Err(SupervisorFailure::new(
            FailureKind::Cancelled,
            "run was cancelled before harness spawn",
        ));
    }
    if spec.environment.ambient_contains(RECURSION_MARKER) {
        return Err(SupervisorFailure::new(
            FailureKind::RecursiveInvocation,
            "recursion marker is already present in the runner environment",
        ));
    }
    let cwd = fs::canonicalize(&spec.cwd).map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "canonical harness working directory is unavailable",
        )
    })?;
    if !cwd.is_dir() {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "canonical harness working directory is not a directory",
        ));
    }
    let executable = resolve_executable(&spec.executable)?;
    if let Some(wrapper) = spec.wrapper_executable.as_ref() {
        if let Ok(wrapper) = fs::canonicalize(wrapper) {
            if wrapper == executable {
                return Err(SupervisorFailure::new(
                    FailureKind::RecursiveInvocation,
                    "selected harness resolves to the prose wrapper executable",
                ));
            }
        }
    }
    spec.environment = spec
        .environment
        .set(RECURSION_MARKER, &spec.recursion_token, Sensitivity::Secret)
        .set(RUN_NONCE, &spec.run_nonce, Sensitivity::Secret)
        .set(INVOCATION_ID, &spec.invocation_id, Sensitivity::Public);
    let secret_values = spec.environment.secret_strings();

    let probed_version = match &spec.version_probe {
        Some(probe) => Some(run_version_probe(
            &executable,
            &cwd,
            &spec.environment,
            probe,
            &spec.cancellation,
            spec.termination_grace,
        )?),
        None => None,
    };

    let mut command = Command::new(&executable);
    command
        .args(&spec.argv)
        .current_dir(&cwd)
        .env_clear()
        .stdin(if spec.stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (name, value) in spec.environment.entries() {
        command.env(name, value);
    }
    let containment = platform::prepare(&mut command).map_err(|error| {
        SupervisorFailure::new(FailureKind::ContainmentUnsupported, error.to_string())
    })?;
    let mut child = command.spawn().map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "selected harness executable could not be started",
        )
    })?;
    let stdin_writer = if let Some(bytes) = spec.stdin.take() {
        let Some(stdin) = child.stdin.take() else {
            let cleanup =
                platform::terminate_original_process_group(&mut child, spec.termination_grace);
            return Err(if cleanup.is_ok() {
                SupervisorFailure::new(FailureKind::Internal, "harness stdin pipe is unavailable")
            } else {
                cleanup_failure("harness setup cleanup could not be verified")
            });
        };
        let Ok(writer) = spawn_stdin_writer(stdin, bytes, spec.stdin_lifecycle) else {
            let cleanup =
                platform::terminate_original_process_group(&mut child, spec.termination_grace);
            return Err(if cleanup.is_ok() {
                SupervisorFailure::new(
                    FailureKind::ContainmentUnsupported,
                    "harness stdin could not be made safely interruptible",
                )
            } else {
                cleanup_failure("harness setup cleanup could not be verified")
            });
        };
        Some(writer)
    } else {
        None
    };
    let Some(stdout) = child.stdout.take() else {
        request_stdin_stop(stdin_writer.as_ref());
        let cleanup =
            platform::terminate_original_process_group(&mut child, spec.termination_grace);
        let stdin_ok = stdin_writer.is_none_or(|writer| {
            writer
                .settle(PROBE_READER_STOP_TIMEOUT)
                .is_some_and(|(_, forced)| !forced)
        });
        return Err(if cleanup.is_ok() && stdin_ok {
            SupervisorFailure::new(FailureKind::Internal, "harness stdout pipe is unavailable")
        } else {
            cleanup_failure("harness setup cleanup could not be verified")
        });
    };
    let Some(stderr) = child.stderr.take() else {
        request_stdin_stop(stdin_writer.as_ref());
        let cleanup =
            platform::terminate_original_process_group(&mut child, spec.termination_grace);
        let stdin_ok = stdin_writer.is_none_or(|writer| {
            writer
                .settle(PROBE_READER_STOP_TIMEOUT)
                .is_some_and(|(_, forced)| !forced)
        });
        return Err(if cleanup.is_ok() && stdin_ok {
            SupervisorFailure::new(FailureKind::Internal, "harness stderr pipe is unavailable")
        } else {
            cleanup_failure("harness setup cleanup could not be verified")
        });
    };
    let queue_size = spec.limits.max_queued_records.max(1);
    let (sender, receiver) = sync_channel(queue_size);
    let stdout_sender = sender.clone();
    let limits = spec.limits;
    let Ok(stdout_reader) = spawn_jsonl_reader(stdout, limits, stdout_sender) else {
        request_stdin_stop(stdin_writer.as_ref());
        let cleanup =
            platform::terminate_original_process_group(&mut child, spec.termination_grace);
        let stdin_ok = stdin_writer.is_none_or(|writer| {
            writer
                .settle(PROBE_READER_STOP_TIMEOUT)
                .is_some_and(|(_, forced)| !forced)
        });
        return Err(if cleanup.is_ok() && stdin_ok {
            SupervisorFailure::new(
                FailureKind::ContainmentUnsupported,
                "harness stdout could not be made safely interruptible",
            )
        } else {
            cleanup_failure("harness setup cleanup could not be verified")
        });
    };
    let stderr_sender = sender.clone();
    let Ok(stderr_reader) = spawn_stderr_reader(stderr, limits, stderr_sender) else {
        request_stdin_stop(stdin_writer.as_ref());
        let cleanup =
            platform::terminate_original_process_group(&mut child, spec.termination_grace);
        drop(receiver);
        let stdout_ok = stdout_reader
            .settle(PROBE_READER_STOP_TIMEOUT)
            .is_some_and(|forced| !forced);
        let stdin_ok = stdin_writer.is_none_or(|writer| {
            writer
                .settle(PROBE_READER_STOP_TIMEOUT)
                .is_some_and(|(_, forced)| !forced)
        });
        return Err(if cleanup.is_ok() && stdout_ok && stdin_ok {
            SupervisorFailure::new(
                FailureKind::ContainmentUnsupported,
                "harness stderr could not be made safely interruptible",
            )
        } else {
            cleanup_failure("harness setup cleanup could not be verified")
        });
    };
    drop(sender);

    let started_at = Instant::now();
    let mut state = ProtocolState::default();
    let mut stderr_bytes = Vec::new();
    let mut stdout_eof = false;
    let mut stderr_eof = false;
    let mut process_status = None;
    let mut failure = None;
    let mut cancellation_scheduled = false;

    while failure.is_none() {
        if process_status.is_none() {
            if let Ok(status) = child.try_wait() {
                process_status = status;
            } else {
                failure = Some(SupervisorFailure::new(
                    FailureKind::Internal,
                    "cannot inspect harness process",
                ));
                break;
            }
        }
        if spec.cancellation.is_cancelled() {
            failure = Some(SupervisorFailure::new(
                FailureKind::Cancelled,
                "run was cancelled before harness completion",
            ));
            break;
        }
        let elapsed = started_at.elapsed();
        if !state.started && elapsed >= spec.startup_timeout {
            failure = Some(SupervisorFailure::new(
                FailureKind::StartupTimeout,
                "harness did not emit its startup record before the deadline",
            ));
            break;
        }
        if elapsed >= spec.run_timeout {
            failure = Some(SupervisorFailure::new(
                FailureKind::RunTimeout,
                "harness run exceeded its configured deadline",
            ));
            break;
        }
        match receiver.recv_timeout(Duration::from_millis(5)) {
            Ok(message) => {
                if let Err(error) = handle_message(
                    message,
                    protocol,
                    &mut state,
                    &mut stderr_bytes,
                    &mut stdout_eof,
                    &mut stderr_eof,
                    &mut observer,
                ) {
                    failure = Some(error);
                }
                if failure.is_none() {
                    if let Some(observer) = observer.as_deref_mut() {
                        match observer.take_stdin_write() {
                            Ok(Some(bytes)) => match stdin_writer.as_ref() {
                                Some(writer) => {
                                    if let Err(error) = writer.request_write(bytes) {
                                        failure = Some(error);
                                    }
                                }
                                None => {
                                    failure = Some(SupervisorFailure::new(
                                        FailureKind::Internal,
                                        "staged adapter stdin pipe is unavailable",
                                    ));
                                }
                            },
                            Ok(None) => {}
                            Err(error) => failure = Some(error),
                        }
                        if observer.close_stdin_requested() {
                            if let Some(writer) = stdin_writer.as_ref() {
                                writer.request_close();
                            }
                        }
                    }
                }
                if failure.is_none() && state.terminal.is_some() && !protocol.terminal_is_candidate
                {
                    if let Some(writer) = stdin_writer.as_ref() {
                        writer.request_close();
                    }
                }
                if state.started && !cancellation_scheduled {
                    if let Some(delay) = spec.cancel_after_start {
                        let scheduled = spec.cancellation.clone();
                        thread::spawn(move || {
                            thread::sleep(delay);
                            scheduled.cancel();
                        });
                    }
                    cancellation_scheduled = true;
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                stdout_eof = true;
                stderr_eof = true;
            }
        }
        if process_status.is_some() && stdout_eof && stderr_eof {
            break;
        }
    }

    if let Some(mut error) = failure {
        let unsupported_nonterminal = error.kind == FailureKind::HarnessFailed
            && error.message == "unsupported_nonterminal_settlement";
        request_stdin_stop(stdin_writer.as_ref());
        let natural_status = process_status.take().or_else(|| {
            (!unsupported_nonterminal
                && matches!(
                    error.kind,
                    FailureKind::ProtocolMalformed
                        | FailureKind::ProtocolTruncated
                        | FailureKind::HarnessFailed
                ))
            .then(|| wait_for_natural_process_exit(&mut child, PROBE_READER_STOP_TIMEOUT))
            .flatten()
        });
        let cleanup =
            platform::terminate_original_process_group(&mut child, spec.termination_grace);
        drain_diagnostics(&receiver, &mut stderr_bytes, spec.limits.max_stderr_bytes);
        let (stdout_settlement, stderr_settlement) = settle_readers_with_diagnostics(
            stdout_reader,
            stderr_reader,
            &receiver,
            &mut stderr_bytes,
            spec.limits.max_stderr_bytes,
            PROBE_READER_STOP_TIMEOUT,
        );
        drain_diagnostics(&receiver, &mut stderr_bytes, spec.limits.max_stderr_bytes);
        drop(receiver);
        let stdin_settlement = stdin_writer.map(|writer| writer.settle(PROBE_READER_STOP_TIMEOUT));
        let readers_settled =
            matches!(stdout_settlement, Some(false)) && matches!(stderr_settlement, Some(false));
        let stdin_settled = stdin_settlement
            .as_ref()
            .is_none_or(|settlement| settlement.as_ref().is_some_and(|(_, forced)| !forced));
        error.stderr = sanitize_diagnostic(&stderr_bytes, &secret_values);
        if let Some(status) = natural_status.or_else(|| child.try_wait().ok().flatten()) {
            (error.process_exit, error.process_signal) = exit_parts(status);
        }
        error.process_started = true;
        error.terminal_observed = !unsupported_nonterminal && state.terminal.is_some();
        error.terminal_envelope = (!unsupported_nonterminal)
            .then(|| state.terminal.clone())
            .flatten();
        error.records.clone_from(&state.records);
        if cleanup.is_err() || !readers_settled || !stdin_settled {
            return Err(SupervisorFailure {
                transport_diagnostic: None,
                kind: FailureKind::CleanupFailed,
                message: "process-group and pipe cleanup could not be verified".to_owned(),
                stderr: error.stderr,
                process_exit: error.process_exit,
                process_signal: error.process_signal,
                process_started: true,
                terminal_observed: error.terminal_observed,
                terminal_envelope: error.terminal_envelope,
                records: error.records,
            });
        }
        return Err(error);
    }

    drop(receiver);
    let stdout_settlement = stdout_reader.settle(PROBE_READER_STOP_TIMEOUT);
    let stderr_settlement = stderr_reader.settle(PROBE_READER_STOP_TIMEOUT);
    let stdin_settlement = stdin_writer.map(|writer| writer.settle(PROBE_READER_STOP_TIMEOUT));
    let readers_settled =
        matches!(stdout_settlement, Some(false)) && matches!(stderr_settlement, Some(false));
    let stdin_settled = stdin_settlement
        .as_ref()
        .is_none_or(|settlement| settlement.as_ref().is_some_and(|(_, forced)| !forced));
    let stdin_ok = stdin_settlement.is_none_or(|settlement| {
        settlement.is_some_and(|(result, forced)| !forced && result.is_ok())
    });
    let status = match process_status {
        Some(status) => status,
        None => child.wait().map_err(|_| {
            SupervisorFailure::new(
                FailureKind::Internal,
                "cannot collect harness status after stream completion",
            )
        })?,
    };
    let diagnostic = sanitize_diagnostic(&stderr_bytes, &secret_values);
    let (process_exit, process_signal) = exit_parts(status);
    if !readers_settled || !stdin_settled {
        let _ = platform::terminate_original_process_group(&mut child, spec.termination_grace);
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::CleanupFailed,
            message: "harness pipe cleanup could not be verified".to_owned(),
            stderr: diagnostic,
            process_exit,
            process_signal,
            process_started: true,
            terminal_observed: state.terminal.is_some(),
            terminal_envelope: state.terminal,
            records: state.records,
        });
    }
    if !platform::original_process_group_is_empty(&child) {
        let _ = platform::terminate_original_process_group(&mut child, spec.termination_grace);
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::CleanupFailed,
            message: "original process group remained after harness exit".to_owned(),
            stderr: diagnostic,
            process_exit,
            process_signal,
            process_started: true,
            terminal_observed: state.terminal.is_some(),
            terminal_envelope: state.terminal,
            records: state.records,
        });
    }
    if !stdin_ok {
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::HarnessFailed,
            message: "adapter input could not be delivered to the harness".to_owned(),
            stderr: diagnostic,
            process_exit,
            process_signal,
            process_started: true,
            terminal_observed: state.terminal.is_some(),
            terminal_envelope: state.terminal,
            records: state.records,
        });
    }
    let Some(terminal_envelope) = state.terminal else {
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::ProtocolTruncated,
            message: "harness stream ended without its required terminal record".to_owned(),
            stderr: diagnostic,
            process_exit,
            process_signal,
            process_started: true,
            terminal_observed: false,
            terminal_envelope: None,
            records: state.records,
        });
    };
    if !status.success() {
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::HarnessFailed,
            message: "harness exited unsuccessfully after a terminal record".to_owned(),
            stderr: diagnostic,
            process_exit,
            process_signal,
            process_started: true,
            terminal_observed: true,
            terminal_envelope: Some(terminal_envelope),
            records: state.records,
        });
    }
    Ok(ProcessOutcome {
        executable,
        cwd,
        records: state.records,
        terminal_envelope,
        stderr: diagnostic,
        process_exit: process_exit.ok_or_else(|| {
            SupervisorFailure::new(
                FailureKind::Internal,
                "successful harness status did not contain an exit code",
            )
        })?,
        process_signal,
        containment,
        probed_version,
        duration: started_at.elapsed(),
    })
}

struct ProbeReader {
    receiver: Receiver<std::io::Result<Vec<u8>>>,
    stop: Arc<AtomicBool>,
    handle: JoinHandle<()>,
}

struct InterruptibleReader<R> {
    stream: R,
    stop: Arc<AtomicBool>,
}

impl<R: std::io::Read> std::io::Read for InterruptibleReader<R> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        loop {
            if self.stop.load(Ordering::Acquire) {
                return Ok(0);
            }
            match self.stream.read(buffer) {
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::park_timeout(PROBE_READER_POLL_INTERVAL);
                }
                result => return result,
            }
        }
    }
}

struct ManagedReader {
    stop: Arc<AtomicBool>,
    handle: JoinHandle<()>,
}

struct ManagedStdin {
    stop: Arc<AtomicBool>,
    close: Arc<AtomicBool>,
    sender: SyncSender<Vec<u8>>,
    handle: JoinHandle<std::io::Result<()>>,
}

impl ManagedStdin {
    fn request_write(&self, bytes: Vec<u8>) -> Result<(), SupervisorFailure> {
        if bytes.is_empty() {
            return Err(SupervisorFailure::new(
                FailureKind::Internal,
                "staged adapter stdin frame was empty",
            ));
        }
        self.sender.try_send(bytes).map_err(|error| match error {
            TrySendError::Full(_) => SupervisorFailure::new(
                FailureKind::Internal,
                "staged adapter stdin queue exceeded its fixed bound",
            ),
            TrySendError::Disconnected(_) => SupervisorFailure::new(
                FailureKind::HarnessFailed,
                "staged adapter stdin could not be delivered to the harness",
            ),
        })
    }

    fn request_close(&self) {
        self.close.store(true, Ordering::Release);
        self.handle.thread().unpark();
    }

    fn request_stop(&self) {
        self.stop.store(true, Ordering::Release);
        self.handle.thread().unpark();
    }

    fn settle(self, natural_wait: Duration) -> Option<(std::io::Result<()>, bool)> {
        let natural_deadline = Instant::now() + natural_wait;
        while !self.handle.is_finished() && Instant::now() < natural_deadline {
            thread::sleep(Duration::from_millis(1));
        }
        let forced = !self.handle.is_finished();
        if forced {
            self.request_stop();
        }
        let stop_deadline = Instant::now() + PROBE_READER_STOP_TIMEOUT;
        while !self.handle.is_finished() && Instant::now() < stop_deadline {
            thread::sleep(Duration::from_millis(1));
        }
        if !self.handle.is_finished() {
            return None;
        }
        self.handle.join().ok().map(|result| (result, forced))
    }
}

impl ManagedReader {
    fn settle(self, natural_wait: Duration) -> Option<bool> {
        let natural_deadline = Instant::now() + natural_wait;
        while !self.handle.is_finished() && Instant::now() < natural_deadline {
            thread::sleep(Duration::from_millis(1));
        }
        let forced = !self.handle.is_finished();
        if forced {
            self.stop.store(true, Ordering::Release);
            self.handle.thread().unpark();
        }
        let stop_deadline = Instant::now() + PROBE_READER_STOP_TIMEOUT;
        while !self.handle.is_finished() && Instant::now() < stop_deadline {
            thread::sleep(Duration::from_millis(1));
        }
        if !self.handle.is_finished() || self.handle.join().is_err() {
            return None;
        }
        Some(forced)
    }
}

fn settle_readers_with_diagnostics(
    stdout: ManagedReader,
    stderr: ManagedReader,
    receiver: &Receiver<ReaderMessage>,
    stderr_bytes: &mut Vec<u8>,
    maximum_stderr_bytes: usize,
    natural_wait: Duration,
) -> (Option<bool>, Option<bool>) {
    let natural_deadline = Instant::now() + natural_wait;
    while (!stdout.handle.is_finished() || !stderr.handle.is_finished())
        && Instant::now() < natural_deadline
    {
        if drain_diagnostics(receiver, stderr_bytes, maximum_stderr_bytes) == 0 {
            wait_for_diagnostic(
                receiver,
                stderr_bytes,
                maximum_stderr_bytes,
                natural_deadline,
            );
        } else {
            thread::yield_now();
        }
    }
    drain_diagnostics(receiver, stderr_bytes, maximum_stderr_bytes);

    let stdout_forced = !stdout.handle.is_finished();
    if stdout_forced {
        stdout.stop.store(true, Ordering::Release);
        stdout.handle.thread().unpark();
    }
    let stderr_forced = !stderr.handle.is_finished();
    if stderr_forced {
        stderr.stop.store(true, Ordering::Release);
        stderr.handle.thread().unpark();
    }

    let stop_deadline = Instant::now() + PROBE_READER_STOP_TIMEOUT;
    while (!stdout.handle.is_finished() || !stderr.handle.is_finished())
        && Instant::now() < stop_deadline
    {
        if drain_diagnostics(receiver, stderr_bytes, maximum_stderr_bytes) == 0 {
            wait_for_diagnostic(receiver, stderr_bytes, maximum_stderr_bytes, stop_deadline);
        } else {
            thread::yield_now();
        }
    }
    drain_diagnostics(receiver, stderr_bytes, maximum_stderr_bytes);

    let stdout_settlement = stdout
        .handle
        .is_finished()
        .then(|| stdout.handle.join().ok().map(|()| stdout_forced))
        .flatten();
    let stderr_settlement = stderr
        .handle
        .is_finished()
        .then(|| stderr.handle.join().ok().map(|()| stderr_forced))
        .flatten();
    (stdout_settlement, stderr_settlement)
}

#[cfg(unix)]
fn prepare_interruptible_pipe(stream: &impl std::os::fd::AsFd) -> std::io::Result<()> {
    rustix::io::ioctl_fionbio(stream, true).map_err(Into::into)
}

#[cfg(not(unix))]
fn prepare_interruptible_pipe<T>(_stream: &T) -> std::io::Result<()> {
    Ok(())
}

fn spawn_jsonl_reader(
    stdout: std::process::ChildStdout,
    limits: StreamLimits,
    sender: std::sync::mpsc::SyncSender<ReaderMessage>,
) -> std::io::Result<ManagedReader> {
    prepare_interruptible_pipe(&stdout)?;
    let stop = Arc::new(AtomicBool::new(false));
    let reader_stop = Arc::clone(&stop);
    let handle = thread::spawn(move || {
        read_jsonl(
            InterruptibleReader {
                stream: stdout,
                stop: reader_stop,
            },
            limits,
            &sender,
        );
    });
    Ok(ManagedReader { stop, handle })
}

fn spawn_stderr_reader(
    stderr: std::process::ChildStderr,
    limits: StreamLimits,
    sender: std::sync::mpsc::SyncSender<ReaderMessage>,
) -> std::io::Result<ManagedReader> {
    prepare_interruptible_pipe(&stderr)?;
    let stop = Arc::new(AtomicBool::new(false));
    let reader_stop = Arc::clone(&stop);
    let handle = thread::spawn(move || {
        read_stderr(
            InterruptibleReader {
                stream: stderr,
                stop: reader_stop,
            },
            limits,
            &sender,
        );
    });
    Ok(ManagedReader { stop, handle })
}

fn spawn_stdin_writer(
    mut stdin: std::process::ChildStdin,
    bytes: Vec<u8>,
    lifecycle: StdinLifecycle,
) -> std::io::Result<ManagedStdin> {
    prepare_interruptible_pipe(&stdin)?;
    let stop = Arc::new(AtomicBool::new(false));
    let close = Arc::new(AtomicBool::new(matches!(
        lifecycle,
        StdinLifecycle::CloseAfterWrite
    )));
    let writer_stop = Arc::clone(&stop);
    let writer_close = Arc::clone(&close);
    let (sender, receiver) = sync_channel::<Vec<u8>>(2);
    let handle = thread::spawn(move || {
        let write_frame = |stdin: &mut std::process::ChildStdin,
                           bytes: &[u8],
                           writer_stop: &AtomicBool|
         -> std::io::Result<()> {
            let mut offset = 0;
            while offset < bytes.len() {
                if writer_stop.load(Ordering::Acquire) {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::Interrupted,
                        "stdin delivery was stopped during process settlement",
                    ));
                }
                match stdin.write(&bytes[offset..]) {
                    Ok(0) => return Err(std::io::Error::from(std::io::ErrorKind::WriteZero)),
                    Ok(count) => offset += count,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::park_timeout(PROBE_READER_POLL_INTERVAL);
                    }
                    Err(error) => return Err(error),
                }
            }
            Ok(())
        };
        write_frame(&mut stdin, &bytes, &writer_stop)?;
        if matches!(lifecycle, StdinLifecycle::CloseAfterWrite) {
            return Ok(());
        }
        loop {
            if writer_stop.load(Ordering::Acquire) {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::Interrupted,
                    "stdin retention was stopped during process settlement",
                ));
            }
            if writer_close.load(Ordering::Acquire) {
                return Ok(());
            }
            match receiver.recv_timeout(PROBE_READER_POLL_INTERVAL) {
                Ok(bytes) => write_frame(&mut stdin, &bytes, &writer_stop)?,
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => {
                    thread::park_timeout(PROBE_READER_POLL_INTERVAL);
                }
            }
        }
    });
    Ok(ManagedStdin {
        stop,
        close,
        sender,
        handle,
    })
}

fn request_stdin_stop(writer: Option<&ManagedStdin>) {
    if let Some(writer) = writer {
        writer.request_stop();
    }
}

struct ProbeReaderSettlement {
    output: std::io::Result<Vec<u8>>,
    forced: bool,
}

#[cfg(unix)]
fn spawn_probe_reader<R>(stream: R, maximum: usize) -> std::io::Result<ProbeReader>
where
    R: std::io::Read + std::os::fd::AsFd + Send + 'static,
{
    rustix::io::ioctl_fionbio(&stream, true)?;
    Ok(spawn_configured_probe_reader(stream, maximum))
}

#[cfg(not(unix))]
fn spawn_probe_reader<R>(stream: R, maximum: usize) -> std::io::Result<ProbeReader>
where
    R: std::io::Read + Send + 'static,
{
    // Direct-process supervision is rejected by `platform::prepare` on these
    // targets. Keep this fallback compilable if that policy changes, while the
    // admitted Windows path continues to use the native process host.
    Ok(spawn_configured_probe_reader(stream, maximum))
}

fn spawn_configured_probe_reader<R>(mut stream: R, maximum: usize) -> ProbeReader
where
    R: std::io::Read + Send + 'static,
{
    let stop = Arc::new(AtomicBool::new(false));
    let reader_stop = Arc::clone(&stop);
    let (sender, receiver) = sync_channel(1);
    let handle = thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut buffer = [0_u8; 8192];
        let result = loop {
            match stream.read(&mut buffer) {
                Ok(0) => break Ok(bytes),
                Ok(count) => {
                    let remaining = maximum.saturating_add(1).saturating_sub(bytes.len());
                    bytes.extend_from_slice(&buffer[..count.min(remaining)]);
                    if bytes.len() > maximum {
                        break Ok(bytes);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if reader_stop.load(Ordering::Acquire) {
                        break Ok(bytes);
                    }
                    thread::park_timeout(PROBE_READER_POLL_INTERVAL);
                }
                Err(error) => break Err(error),
            }
        };
        let _ = sender.send(result);
    });
    ProbeReader {
        receiver,
        stop,
        handle,
    }
}

impl ProbeReader {
    fn request_stop(&self) {
        self.stop.store(true, Ordering::Release);
        self.handle.thread().unpark();
    }

    fn settle(self, natural_wait: Duration, force_now: bool) -> Option<ProbeReaderSettlement> {
        let mut forced = force_now;
        if force_now {
            self.request_stop();
        }
        let output = match self.receiver.recv_timeout(if force_now {
            PROBE_READER_STOP_TIMEOUT
        } else {
            natural_wait
        }) {
            Ok(output) => output,
            Err(RecvTimeoutError::Timeout) if !force_now => {
                forced = true;
                self.request_stop();
                self.receiver.recv_timeout(PROBE_READER_STOP_TIMEOUT).ok()?
            }
            Err(RecvTimeoutError::Timeout | RecvTimeoutError::Disconnected) => return None,
        };
        let finish_deadline = Instant::now() + PROBE_READER_STOP_TIMEOUT;
        while !self.handle.is_finished() && Instant::now() < finish_deadline {
            thread::sleep(Duration::from_millis(1));
        }
        if !self.handle.is_finished() || self.handle.join().is_err() {
            return None;
        }
        Some(ProbeReaderSettlement { output, forced })
    }
}

fn cleanup_probe_after_failure(
    child: &mut std::process::Child,
    readers: impl IntoIterator<Item = ProbeReader>,
    termination_grace: Duration,
) -> bool {
    let cleanup_ok = platform::terminate_original_process_group(child, termination_grace).is_ok();
    let mut readers_ok = true;
    for reader in readers {
        readers_ok &= reader.settle(Duration::ZERO, true).is_some();
    }
    cleanup_ok && readers_ok
}

fn cleanup_failure(message: &'static str) -> SupervisorFailure {
    SupervisorFailure::new(FailureKind::CleanupFailed, message)
}

#[allow(clippy::too_many_lines)]
#[cfg_attr(windows, allow(dead_code))]
fn run_version_probe(
    executable: &Path,
    cwd: &Path,
    environment: &EnvironmentPolicy,
    probe: &VersionProbe,
    cancellation: &CancellationToken,
    termination_grace: Duration,
) -> Result<String, SupervisorFailure> {
    let mut command = Command::new(executable);
    command
        .args(&probe.argv)
        .current_dir(cwd)
        .env_clear()
        .stdin(Stdio::null())
        .stdout(if probe.output == VersionProbeOutput::Stdout {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stderr(if probe.output == VersionProbeOutput::Stderr {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    for (name, value) in environment.entries() {
        command.env(name, value);
    }
    platform::prepare(&mut command).map_err(|error| {
        SupervisorFailure::new(FailureKind::ContainmentUnsupported, error.to_string())
    })?;
    let mut child = command.spawn().map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "selected harness version probe could not be started",
        )
    })?;
    let maximum = probe.max_output_bytes;
    let reader = match probe.output {
        VersionProbeOutput::Stdout => child
            .stdout
            .take()
            .map(|stream| spawn_probe_reader(stream, maximum)),
        VersionProbeOutput::Stderr => child
            .stderr
            .take()
            .map(|stream| spawn_probe_reader(stream, maximum)),
    };
    let Some(reader) = reader else {
        let cleanup = platform::terminate_original_process_group(&mut child, termination_grace);
        return Err(if cleanup.is_ok() {
            SupervisorFailure::new(
                FailureKind::Internal,
                "version-probe selected output pipe is unavailable",
            )
        } else {
            cleanup_failure("version-probe setup cleanup could not be verified")
        });
    };
    let Ok(reader) = reader else {
        let cleanup = platform::terminate_original_process_group(&mut child, termination_grace);
        return Err(if cleanup.is_ok() {
            SupervisorFailure::new(
                FailureKind::ContainmentUnsupported,
                "version-probe output could not be made safely interruptible",
            )
        } else {
            cleanup_failure("version-probe setup cleanup could not be verified")
        });
    };
    let deadline = Instant::now() + probe.timeout;
    let status = loop {
        if cancellation.is_cancelled() {
            if !cleanup_probe_after_failure(&mut child, [reader], termination_grace) {
                return Err(cleanup_failure(
                    "cancelled version-probe cleanup could not be verified",
                ));
            }
            return Err(SupervisorFailure::new(
                FailureKind::Cancelled,
                "run was cancelled during harness version probe",
            ));
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(_) => {
                if !cleanup_probe_after_failure(&mut child, [reader], termination_grace) {
                    return Err(cleanup_failure(
                        "failed version-probe inspection cleanup could not be verified",
                    ));
                }
                return Err(SupervisorFailure::new(
                    FailureKind::HarnessIncompatible,
                    "cannot inspect harness version probe",
                ));
            }
        }
        if Instant::now() >= deadline {
            if !cleanup_probe_after_failure(&mut child, [reader], termination_grace) {
                return Err(cleanup_failure(
                    "version-probe containment could not be verified empty",
                ));
            }
            return Err(SupervisorFailure::new(
                FailureKind::StartupTimeout,
                "harness version probe exceeded its deadline",
            ));
        }
        thread::sleep(Duration::from_millis(5));
    };
    if !platform::original_process_group_is_empty(&child) {
        if !cleanup_probe_after_failure(&mut child, [reader], termination_grace) {
            return Err(cleanup_failure(
                "version-probe descendant cleanup could not be verified",
            ));
        }
        return Err(cleanup_failure(
            "version probe retained descendants after exit",
        ));
    }
    let Some(settlement) = reader.settle(termination_grace.max(PROBE_READER_STOP_TIMEOUT), false)
    else {
        return Err(cleanup_failure(
            "version-probe output reader could not be settled",
        ));
    };
    if settlement.forced {
        return Err(cleanup_failure(
            "version probe retained an output pipe after exit",
        ));
    }
    let bytes = settlement.output.map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessIncompatible,
            "harness version output could not be read",
        )
    })?;
    if !status.success() || bytes.is_empty() || bytes.len() > maximum {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessIncompatible,
            "harness returned an invalid bounded version response",
        ));
    }
    let version = std::str::from_utf8(&bytes)
        .map_err(|_| {
            SupervisorFailure::new(
                FailureKind::HarnessIncompatible,
                "harness version response is not UTF-8",
            )
        })?
        .trim()
        .to_owned();
    if version.is_empty()
        || probe
            .required_substring
            .as_ref()
            .is_some_and(|required| !version.contains(required))
    {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessIncompatible,
            "harness version response is outside the adapter admission range",
        ));
    }
    Ok(version)
}

/// Runs a bounded, shell-free version probe without starting a model run.
///
/// This is the discovery half of normal adapter readiness checks. It uses the
/// same closed environment, process-group setup, cancellation, output bound,
/// and descendant cleanup checks as the probe performed immediately before a
/// supervised harness run.
///
/// # Errors
///
/// Returns a closed supervisor failure when the executable or working
/// directory cannot be resolved, the probe is cancelled or times out, its
/// output is invalid, or cleanup cannot be verified.
pub fn probe_version(
    executable: &Path,
    cwd: &Path,
    environment: &EnvironmentPolicy,
    probe: &VersionProbe,
    cancellation: &CancellationToken,
) -> Result<String, SupervisorFailure> {
    let cwd = fs::canonicalize(cwd).map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "canonical harness working directory is unavailable",
        )
    })?;
    if !cwd.is_dir() {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "canonical harness working directory is not a directory",
        ));
    }
    let executable = resolve_executable(executable)?;
    run_version_probe(
        &executable,
        &cwd,
        environment,
        probe,
        cancellation,
        Duration::from_secs(2),
    )
}

/// Runs a bounded, shell-free local readiness command with the adapter's
/// closed environment and process-group cleanup policy.
///
/// # Errors
///
/// Returns a closed supervisor failure for resolution, spawn, timeout,
/// cancellation, output overflow/non-UTF-8, signal exit, or cleanup failure.
#[allow(clippy::too_many_lines)]
pub fn probe_command(
    executable: &Path,
    cwd: &Path,
    environment: &EnvironmentPolicy,
    probe: &CommandProbe,
    cancellation: &CancellationToken,
) -> Result<CommandProbeOutcome, SupervisorFailure> {
    let cwd = fs::canonicalize(cwd).map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "canonical harness working directory is unavailable",
        )
    })?;
    let executable = resolve_executable(executable)?;
    let mut command = Command::new(executable);
    command
        .args(&probe.argv)
        .current_dir(cwd)
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (name, value) in environment.entries() {
        command.env(name, value);
    }
    platform::prepare(&mut command).map_err(|error| {
        SupervisorFailure::new(FailureKind::ContainmentUnsupported, error.to_string())
    })?;
    let mut child = command.spawn().map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "selected harness readiness probe could not be started",
        )
    })?;
    let maximum = probe.max_output_bytes;
    let Some(stdout) = child.stdout.take() else {
        let cleanup =
            platform::terminate_original_process_group(&mut child, Duration::from_secs(2));
        return Err(if cleanup.is_ok() {
            SupervisorFailure::new(
                FailureKind::Internal,
                "readiness-probe stdout is unavailable",
            )
        } else {
            cleanup_failure("readiness-probe setup cleanup could not be verified")
        });
    };
    let Some(stderr) = child.stderr.take() else {
        let cleanup =
            platform::terminate_original_process_group(&mut child, Duration::from_secs(2));
        return Err(if cleanup.is_ok() {
            SupervisorFailure::new(
                FailureKind::Internal,
                "readiness-probe stderr is unavailable",
            )
        } else {
            cleanup_failure("readiness-probe setup cleanup could not be verified")
        });
    };
    let Ok(stdout_reader) = spawn_probe_reader(stdout, maximum) else {
        let cleanup =
            platform::terminate_original_process_group(&mut child, Duration::from_secs(2));
        return Err(if cleanup.is_ok() {
            SupervisorFailure::new(
                FailureKind::ContainmentUnsupported,
                "readiness-probe stdout could not be made safely interruptible",
            )
        } else {
            cleanup_failure("readiness-probe setup cleanup could not be verified")
        });
    };
    let Ok(stderr_reader) = spawn_probe_reader(stderr, maximum) else {
        if !cleanup_probe_after_failure(&mut child, [stdout_reader], Duration::from_secs(2)) {
            return Err(cleanup_failure(
                "readiness-probe setup cleanup could not be verified",
            ));
        }
        return Err(SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "readiness-probe stderr could not be made safely interruptible",
        ));
    };
    let deadline = Instant::now() + probe.timeout;
    let status = loop {
        if cancellation.is_cancelled() {
            if !cleanup_probe_after_failure(
                &mut child,
                [stdout_reader, stderr_reader],
                Duration::from_secs(2),
            ) {
                return Err(cleanup_failure(
                    "cancelled readiness-probe cleanup could not be verified",
                ));
            }
            return Err(SupervisorFailure::new(
                FailureKind::Cancelled,
                "run was cancelled during harness readiness probe",
            ));
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(_) => {
                if !cleanup_probe_after_failure(
                    &mut child,
                    [stdout_reader, stderr_reader],
                    Duration::from_secs(2),
                ) {
                    return Err(cleanup_failure(
                        "failed readiness-probe inspection cleanup could not be verified",
                    ));
                }
                return Err(SupervisorFailure::new(
                    FailureKind::Internal,
                    "cannot inspect harness readiness probe",
                ));
            }
        }
        if Instant::now() >= deadline {
            if !cleanup_probe_after_failure(
                &mut child,
                [stdout_reader, stderr_reader],
                Duration::from_secs(2),
            ) {
                return Err(cleanup_failure(
                    "readiness-probe containment could not be verified empty",
                ));
            }
            return Err(SupervisorFailure::new(
                FailureKind::StartupTimeout,
                "harness readiness probe exceeded its deadline",
            ));
        }
        thread::sleep(Duration::from_millis(5));
    };
    if !platform::original_process_group_is_empty(&child) {
        if !cleanup_probe_after_failure(
            &mut child,
            [stdout_reader, stderr_reader],
            Duration::from_secs(2),
        ) {
            return Err(cleanup_failure(
                "readiness-probe descendant cleanup could not be verified",
            ));
        }
        return Err(cleanup_failure(
            "readiness probe retained descendants after exit",
        ));
    }
    let natural_wait = Duration::from_millis(250);
    let Some(stdout_settlement) = stdout_reader.settle(natural_wait, false) else {
        let _ = stderr_reader.settle(Duration::ZERO, true);
        return Err(cleanup_failure(
            "readiness-probe stdout reader could not be settled",
        ));
    };
    let Some(stderr_settlement) = stderr_reader.settle(natural_wait, false) else {
        return Err(cleanup_failure(
            "readiness-probe stderr reader could not be settled",
        ));
    };
    if stdout_settlement.forced || stderr_settlement.forced {
        return Err(cleanup_failure(
            "readiness probe retained an output pipe after exit",
        ));
    }
    let stdout = stdout_settlement.output.map_err(|_| {
        SupervisorFailure::new(FailureKind::Internal, "cannot read readiness-probe stdout")
    })?;
    let stderr = stderr_settlement.output.map_err(|_| {
        SupervisorFailure::new(FailureKind::Internal, "cannot read readiness-probe stderr")
    })?;
    if stdout.len() > maximum || stderr.len() > maximum {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessIncompatible,
            "harness readiness output exceeded its fixed limit",
        ));
    }
    let (exit_code, signal) = exit_parts(status);
    let exit_code = exit_code.ok_or_else(|| {
        SupervisorFailure::new(
            FailureKind::HarnessIncompatible,
            format!(
                "harness readiness probe exited by {}",
                signal.unwrap_or_else(|| "signal".to_owned())
            ),
        )
    })?;
    let secrets = environment.secret_strings();
    let stdout = std::str::from_utf8(&stdout)
        .map_err(|_| {
            SupervisorFailure::new(
                FailureKind::HarnessIncompatible,
                "harness readiness stdout is not UTF-8",
            )
        })?
        .to_owned();
    let stdout = sanitize_diagnostic(stdout.as_bytes(), &secrets);
    let stderr = sanitize_diagnostic(&stderr, &secrets);
    Ok(CommandProbeOutcome {
        exit_code,
        stdout,
        stderr,
    })
}

pub(crate) fn resolve_executable(path: &Path) -> Result<PathBuf, SupervisorFailure> {
    if !path.is_absolute() {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "adapter must resolve the harness to an absolute path before supervision",
        ));
    }
    let canonical = fs::canonicalize(path).map_err(|_| {
        SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "selected harness executable path is unavailable",
        )
    })?;
    if !canonical.is_file() {
        return Err(SupervisorFailure::new(
            FailureKind::HarnessUnavailable,
            "selected harness executable path is not a file",
        ));
    }
    Ok(canonical)
}

fn handle_message(
    message: ReaderMessage,
    protocol: &JsonlProtocol,
    state: &mut ProtocolState,
    stderr: &mut Vec<u8>,
    stdout_eof: &mut bool,
    stderr_eof: &mut bool,
    observer: &mut Option<&mut dyn RecordObserver>,
) -> Result<(), SupervisorFailure> {
    match message {
        ReaderMessage::Record(bytes) => {
            if let Some(observer) = observer.as_deref_mut() {
                if let Ok(record) = serde_json::from_slice::<Value>(&bytes) {
                    observer.observe_parsed(&record)?;
                }
            }
            state.accept(&bytes, protocol)?;
            if let (Some(observer), Some(record)) = (observer.as_deref_mut(), state.records.last())
            {
                let record = record.clone();
                let observation_result = observer.observe(&record);
                if let Some(projection) = observer.retained_record_projection(&record) {
                    *state
                        .records
                        .last_mut()
                        .expect("the just-admitted record remains present") = projection;
                }
                observation_result?;
            }
            Ok(())
        }
        ReaderMessage::StdoutEof => {
            *stdout_eof = true;
            Ok(())
        }
        ReaderMessage::StdoutTruncated { observed } => Err(SupervisorFailure::new(
            FailureKind::ProtocolTruncated,
            "harness stream ended in the middle of a structured record",
        )
        .with_observed_diagnostic("truncated-record", observed)),
        ReaderMessage::StdoutLimit {
            record,
            observed,
            limit,
        } => Err(SupervisorFailure::new(
            FailureKind::ProtocolMalformed,
            if record {
                "harness structured output exceeded a fixed record limit"
            } else {
                "harness structured output exceeded a fixed transport limit"
            },
        )
        .with_byte_diagnostic(record, observed, limit)),
        ReaderMessage::StdoutIo => Err(SupervisorFailure::new(
            FailureKind::ProtocolTruncated,
            "harness structured output could not be read to completion",
        )),
        ReaderMessage::Stderr(bytes) => {
            stderr.extend_from_slice(&bytes);
            Ok(())
        }
        ReaderMessage::StderrEof => {
            *stderr_eof = true;
            Ok(())
        }
        ReaderMessage::StderrLimit => Err(SupervisorFailure::new(
            FailureKind::HarnessFailed,
            "harness diagnostic output exceeded a fixed transport limit",
        )),
        ReaderMessage::StderrIo => Err(SupervisorFailure::new(
            FailureKind::HarnessFailed,
            "harness diagnostic output could not be read to completion",
        )),
    }
}

// Wake when a blocked sender supplies data instead of sleeping after every
// empty poll. A one-slot channel must not incur 1 ms per diagnostic record.
fn wait_for_diagnostic(
    receiver: &Receiver<ReaderMessage>,
    stderr: &mut Vec<u8>,
    maximum: usize,
    deadline: Instant,
) {
    let timeout = deadline
        .saturating_duration_since(Instant::now())
        .min(Duration::from_millis(1));
    if let Ok(ReaderMessage::Stderr(bytes)) = receiver.recv_timeout(timeout) {
        let remaining = maximum.saturating_sub(stderr.len());
        stderr.extend_from_slice(&bytes[..bytes.len().min(remaining)]);
    }
}

fn drain_diagnostics(
    receiver: &Receiver<ReaderMessage>,
    stderr: &mut Vec<u8>,
    maximum: usize,
) -> usize {
    let mut drained = 0;
    while let Ok(message) = receiver.try_recv() {
        drained += 1;
        if let ReaderMessage::Stderr(bytes) = message {
            let remaining = maximum.saturating_sub(stderr.len());
            stderr.extend_from_slice(&bytes[..bytes.len().min(remaining)]);
        }
    }
    drained
}

pub(crate) fn sanitize_diagnostic(bytes: &[u8], secrets: &[String]) -> String {
    let mut diagnostic = String::from_utf8_lossy(bytes).into_owned();
    for secret in secrets {
        diagnostic = diagnostic.replace(secret, "[REDACTED]");
    }
    diagnostic
}

fn exit_parts(status: ExitStatus) -> (Option<i32>, Option<String>) {
    let exit = status.code();
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt as _;
        let signal = status.signal().map(|signal| match signal {
            2 => "SIGINT".to_owned(),
            9 => "SIGKILL".to_owned(),
            15 => "SIGTERM".to_owned(),
            other => format!("SIG{other}"),
        });
        (exit, signal)
    }
    #[cfg(not(unix))]
    {
        (exit, None)
    }
}

fn wait_for_natural_process_exit(child: &mut Child, maximum: Duration) -> Option<ExitStatus> {
    let deadline = Instant::now() + maximum;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => {
                thread::sleep(PROBE_READER_POLL_INTERVAL);
            }
            Ok(None) | Err(_) => return None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct WouldBlockForever;

    impl std::io::Read for WouldBlockForever {
        fn read(&mut self, _buffer: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::Error::from(std::io::ErrorKind::WouldBlock))
        }
    }

    struct ReadFailure;

    impl std::io::Read for ReadFailure {
        fn read(&mut self, _buffer: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::Error::other("fixture read failure"))
        }
    }

    #[test]
    fn probe_reader_forces_a_nonclosing_stream_to_settle_within_its_bound() {
        let started = Instant::now();
        let settlement = spawn_configured_probe_reader(WouldBlockForever, 64)
            .settle(Duration::from_millis(10), false)
            .unwrap();
        assert!(settlement.forced);
        assert_eq!(settlement.output.unwrap(), Vec::<u8>::new());
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn probe_reader_reports_read_errors_without_leaking_its_worker() {
        let settlement = spawn_configured_probe_reader(ReadFailure, 64)
            .settle(Duration::from_millis(50), false)
            .unwrap();
        assert!(!settlement.forced);
        assert_eq!(
            settlement.output.unwrap_err().kind(),
            std::io::ErrorKind::Other
        );
    }

    #[test]
    fn failure_settlement_drains_diagnostics_under_channel_backpressure() {
        let (sender, receiver) = sync_channel(1);
        let stdout_stop = Arc::new(AtomicBool::new(false));
        let stdout_sender = sender.clone();
        let stdout = ManagedReader {
            stop: Arc::clone(&stdout_stop),
            handle: thread::spawn(move || {
                stdout_sender.send(ReaderMessage::StdoutEof).unwrap();
            }),
        };
        let stderr_stop = Arc::new(AtomicBool::new(false));
        let stderr_sender = sender.clone();
        let stderr = ManagedReader {
            stop: Arc::clone(&stderr_stop),
            handle: thread::spawn(move || {
                for _ in 0..512 {
                    stderr_sender
                        .send(ReaderMessage::Stderr(vec![b'x'; 8_192]))
                        .unwrap();
                }
                stderr_sender.send(ReaderMessage::StderrEof).unwrap();
            }),
        };
        drop(sender);
        let mut diagnostic = Vec::new();

        let settlements = settle_readers_with_diagnostics(
            stdout,
            stderr,
            &receiver,
            &mut diagnostic,
            8 * 1024 * 1024,
            PROBE_READER_STOP_TIMEOUT,
        );

        assert_eq!(settlements, (Some(false), Some(false)));
        assert_eq!(diagnostic.len(), 4 * 1024 * 1024);
        assert!(diagnostic.iter().all(|byte| *byte == b'x'));
        assert!(!stdout_stop.load(Ordering::Acquire));
        assert!(!stderr_stop.load(Ordering::Acquire));
    }

    #[test]
    fn diagnostic_reasons_are_closed_and_payload_free() {
        let cases: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/transport-diagnostics.json"
        ))
        .unwrap();
        let mut state = ProtocolState::default();
        let input = cases
            .as_array()
            .unwrap()
            .iter()
            .find(|x| x["name"] == "json")
            .unwrap()["input"]
            .as_str()
            .unwrap()
            .trim();
        let error = state
            .accept(input.as_bytes(), &JsonlProtocol::fake_harness())
            .unwrap_err();
        let diagnostic = error.transport_diagnostic().unwrap();
        assert_eq!(diagnostic["reason"], "invalid-json");
        assert!(!diagnostic.to_string().contains("secret-invalid"));
        let error = state
            .accept(&[255], &JsonlProtocol::fake_harness())
            .unwrap_err();
        assert_eq!(
            error.transport_diagnostic().unwrap()["reason"],
            "invalid-utf8"
        );
        let error = SupervisorFailure::new(FailureKind::ProtocolMalformed, "untrusted raw payload");
        assert_eq!(
            error.transport_diagnostic().unwrap()["reason"],
            "lifecycle-rejection"
        );
        let error = SupervisorFailure::new(FailureKind::ProtocolMalformed, "limit")
            .with_byte_diagnostic(false, usize::MAX, 8);
        assert_eq!(
            error.transport_diagnostic().unwrap()["observedBytes"],
            u32::MAX
        );
        assert_eq!(error.transport_diagnostic().unwrap()["saturated"], true);
    }

    #[test]
    fn prime_diagnostic_framing_classification_is_closed_to_transport_failures() {
        for (kind, message) in [
            (
                FailureKind::ProtocolMalformed,
                "harness emitted a malformed JSONL record",
            ),
            (
                FailureKind::ProtocolMalformed,
                "harness emitted a non-object JSONL record",
            ),
            (
                FailureKind::ProtocolMalformed,
                "harness emitted an empty structured record",
            ),
            (
                FailureKind::ProtocolMalformed,
                "harness structured output exceeded a fixed record limit",
            ),
            (
                FailureKind::ProtocolMalformed,
                "harness structured output exceeded a fixed transport limit",
            ),
            (
                FailureKind::ProtocolTruncated,
                "harness stream ended in the middle of a structured record",
            ),
            (
                FailureKind::ProtocolTruncated,
                "harness structured output could not be read to completion",
            ),
        ] {
            assert!(SupervisorFailure::new(kind, message).protocol_failure_is_framing());
        }

        for (kind, message) in [
            (
                FailureKind::ProtocolMalformed,
                "Prime lifecycle carried candidate-specific text",
            ),
            (
                FailureKind::ProtocolTruncated,
                "harness exited before the terminal structured record",
            ),
            (
                FailureKind::HarnessFailed,
                "harness emitted a malformed JSONL record",
            ),
        ] {
            assert!(!SupervisorFailure::new(kind, message).protocol_failure_is_framing());
        }
    }

    #[test]
    fn duplicate_and_reordered_records_fail_closed() {
        let protocol = JsonlProtocol::fake_harness();
        let start = br#"{"schema":"openprose.fake-harness-event/1","type":"session.started"}"#;
        let message = br#"{"schema":"openprose.fake-harness-event/1","type":"assistant.message"}"#;
        let mut state = ProtocolState::default();
        assert_eq!(
            state.accept(message, &protocol).unwrap_err().kind,
            FailureKind::ProtocolMalformed
        );
        state.accept(start, &protocol).unwrap();
        assert_eq!(
            state.accept(start, &protocol).unwrap_err().kind,
            FailureKind::ProtocolMalformed
        );
    }

    #[test]
    fn parsed_capture_retains_rejected_record_without_admission() {
        struct Capture(Vec<Value>);
        impl RecordObserver for Capture {
            fn observe_parsed(&mut self, record: &Value) -> Result<(), SupervisorFailure> {
                self.0.push(record.clone());
                Ok(())
            }
            fn observe(&mut self, _: &Value) -> Result<(), SupervisorFailure> {
                Ok(())
            }
        }
        let protocol = JsonlProtocol::installed("ready", "done", ["message"]);
        let mut state = ProtocolState::default();
        let mut capture = Capture(vec![]);
        let mut stderr = vec![];
        let mut stdout_eof = false;
        let mut stderr_eof = false;
        for bytes in [
            br#"{"type":"ready"}"#.to_vec(),
            br#"{"type":"unexpected","text":"fixture-secret"}"#.to_vec(),
        ] {
            let result = handle_message(
                ReaderMessage::Record(bytes),
                &protocol,
                &mut state,
                &mut stderr,
                &mut stdout_eof,
                &mut stderr_eof,
                &mut Some(&mut capture),
            );
            if capture.0.len() == 2 {
                assert_eq!(result.unwrap_err().kind, FailureKind::ProtocolMalformed);
            } else {
                result.unwrap();
            }
        }
        assert_eq!(capture.0.len(), 2);
        assert_eq!(state.records.len(), 1);
        assert!(state.terminal.is_none());
    }

    #[test]
    fn observer_projection_replaces_candidate_data_even_when_observation_fails() {
        struct RejectingProjection;

        impl RecordObserver for RejectingProjection {
            fn observe(&mut self, _record: &Value) -> Result<(), SupervisorFailure> {
                Err(SupervisorFailure::new(
                    FailureKind::HarnessFailed,
                    "projected fixture rejection",
                ))
            }

            fn retained_record_projection(&self, _record: &Value) -> Option<Value> {
                Some(serde_json::json!({
                    "type":"response",
                    "data":{"dumpTools":[]}
                }))
            }
        }

        let protocol = JsonlProtocol::installed("ready", "agent_end", ["response"]);
        let mut state = ProtocolState::default();
        state.accept(br#"{"type":"ready"}"#, &protocol).unwrap();
        let mut stderr = Vec::new();
        let mut stdout_eof = false;
        let mut stderr_eof = false;
        let mut implementation = RejectingProjection;
        let mut observer: Option<&mut dyn RecordObserver> = Some(&mut implementation);
        let failure = handle_message(
            ReaderMessage::Record(
                br#"{"type":"response","data":{"candidateSecret":"OPENPROSE-CANDIDATE-CANARY::state"}}"#
                    .to_vec(),
            ),
            &protocol,
            &mut state,
            &mut stderr,
            &mut stdout_eof,
            &mut stderr_eof,
            &mut observer,
        )
        .unwrap_err();

        assert_eq!(failure.kind, FailureKind::HarnessFailed);
        assert_eq!(
            state.records.last(),
            Some(&serde_json::json!({"type":"response","data":{"dumpTools":[]}}))
        );
        assert!(
            !serde_json::to_string(&state.records)
                .unwrap()
                .contains("OPENPROSE-CANDIDATE-CANARY")
        );
    }

    #[test]
    fn explicitly_allowed_start_type_can_carry_later_nonterminal_records() {
        let protocol = JsonlProtocol::installed("system", "result", ["system", "assistant"]);
        let mut state = ProtocolState::default();
        state
            .accept(br#"{"type":"system","subtype":"init"}"#, &protocol)
            .unwrap();
        state
            .accept(
                br#"{"type":"system","subtype":"thinking_tokens"}"#,
                &protocol,
            )
            .unwrap();
        assert!(state.terminal.is_none());
        state.accept(br#"{"type":"result"}"#, &protocol).unwrap();
        assert!(state.terminal.is_some());
        let strict = JsonlProtocol::installed("system", "result", ["assistant"]);
        let mut state = ProtocolState::default();
        state.accept(br#"{"type":"system"}"#, &strict).unwrap();
        assert!(state.accept(br#"{"type":"system"}"#, &strict).is_err());
    }

    #[test]
    fn installed_protocol_retains_the_whole_terminal_record() {
        let protocol = JsonlProtocol::installed(
            "thread.started",
            "turn.completed",
            ["turn.started", "item.completed"],
        );
        let mut state = ProtocolState::default();
        state
            .accept(
                br#"{"type":"thread.started","thread_id":"fixture"}"#,
                &protocol,
            )
            .unwrap();
        state
            .accept(br#"{"type":"turn.started"}"#, &protocol)
            .unwrap();
        state
            .accept(
                br#"{"type":"turn.completed","usage":{"input_tokens":1}}"#,
                &protocol,
            )
            .unwrap();
        assert_eq!(state.terminal.unwrap()["type"], "turn.completed");
    }

    #[test]
    fn installed_protocol_can_close_stdin_at_lifecycle_end_then_accept_only_a_late_ack() {
        let protocol = JsonlProtocol::installed("ready", "agent_end", ["agent_start", "response"])
            .with_allowed_after_terminal_events(["response"]);
        let mut state = ProtocolState::default();
        state.accept(br#"{"type":"ready"}"#, &protocol).unwrap();
        state
            .accept(br#"{"type":"agent_start"}"#, &protocol)
            .unwrap();
        state.accept(br#"{"type":"agent_end"}"#, &protocol).unwrap();
        state
            .accept(
                br#"{"type":"response","id":"fixture","command":"prompt","success":true}"#,
                &protocol,
            )
            .unwrap();
        assert_eq!(state.records.len(), 4);
        assert_eq!(state.terminal.as_ref().unwrap()["type"], "agent_end");
        assert_eq!(
            state
                .accept(br#"{"type":"agent_start"}"#, &protocol)
                .unwrap_err()
                .kind,
            FailureKind::ProtocolMalformed
        );
    }

    #[test]
    fn host_chunk_router_preserves_fragmented_crlf_jsonl_records() {
        let protocol = JsonlProtocol::fake_harness();
        let mut state = ProtocolState::default();
        let mut assembler = HarnessJsonlAssembler::new(1024);
        assembler
            .push(
                br#"{"schema":"openprose.fake-harness-event/1","type":"session."#,
                &mut state,
                &protocol,
            )
            .unwrap();
        assembler
            .push(
                b"started\"}\r\n{\"schema\":\"openprose.fake-harness-event/1\",\"type\":\"session.completed\",\"terminalEnvelope\":{}}\n",
                &mut state,
                &protocol,
            )
            .unwrap();
        assembler.finish().unwrap();
        assert!(state.started);
        assert!(state.terminal.is_some());
        assert_eq!(state.records.len(), 2);
    }

    #[test]
    fn host_chunk_router_rejects_partial_empty_and_oversized_records() {
        let protocol = JsonlProtocol::fake_harness();
        let mut state = ProtocolState::default();
        let mut partial = HarnessJsonlAssembler::new(4);
        partial.push(b"abc", &mut state, &protocol).unwrap();
        assert_eq!(
            partial.finish().unwrap_err().kind,
            FailureKind::ProtocolTruncated
        );

        let mut empty = HarnessJsonlAssembler::new(4);
        assert_eq!(
            empty.push(b"\n", &mut state, &protocol).unwrap_err().kind,
            FailureKind::ProtocolMalformed
        );
        let mut oversized = HarnessJsonlAssembler::new(2);
        assert_eq!(
            oversized
                .push(b"abc", &mut state, &protocol)
                .unwrap_err()
                .kind,
            FailureKind::ProtocolMalformed
        );
    }
}

#[cfg(test)]
mod candidate_terminal_tests {
    use super::*;
    #[test]
    fn candidate_requires_fresh_native_record_and_preserves_legacy() {
        let records: Vec<Value> = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/adapters/claude-native-turns.json"
        ))
        .unwrap();
        let mut protocol = JsonlProtocol::installed("system", "result", ["system", "assistant"]);
        protocol.terminal_is_candidate = true;
        let mut state = ProtocolState::default();
        for r in &records {
            state
                .accept(&serde_json::to_vec(r).unwrap(), &protocol)
                .unwrap();
        }
        assert!(state.terminal.is_some());
        assert_eq!(state.records.len(), 3);
        state.accept(br#"{"type":"assistant"}"#, &protocol).unwrap();
        assert!(state.terminal.is_some()); // Candidate evidence retained; adapter validates freshness after exit.
        state
            .accept(&serde_json::to_vec(&records[2]).unwrap(), &protocol)
            .unwrap();
        assert!(state.terminal.is_some());
        protocol.terminal_is_candidate = false;
        let mut state = ProtocolState::default();
        for r in &records[..2] {
            state
                .accept(&serde_json::to_vec(r).unwrap(), &protocol)
                .unwrap();
        }
        assert!(
            state
                .accept(&serde_json::to_vec(&records[2]).unwrap(), &protocol)
                .is_err()
        );
    }
}
