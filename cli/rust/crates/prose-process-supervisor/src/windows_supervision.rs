use crate::environment::Sensitivity;
use crate::framing::{ReaderMessage, StreamLimits, read_jsonl, read_stderr};
use crate::supervisor::{
    FailureKind, HarnessJsonlAssembler, JsonlProtocol, ProcessOutcome, ProcessSpec, ProtocolState,
    RecordObserver, SupervisorFailure, VersionProbeOutput, sanitize_diagnostic,
};
use crate::windows_host::{
    VerifiedWindowsHost, WindowsGracefulControl, WindowsHostCancellation, WindowsHostChunk,
    WindowsHostClient, WindowsHostClientFailure, WindowsHostClientFailureKind,
    WindowsHostCompletion, WindowsHostEnvironmentEntry, WindowsHostLimits, WindowsHostRequest,
    encode_windows_cancel_control, encode_windows_host_request, parse_windows_host_bootstrap_error,
    validate_windows_host_identity, validate_windows_version_probe, verify_windows_host_sibling,
};
use crate::{
    INVOCATION_ID, RECURSION_MARKER, RUN_NONCE, compiled_windows_host_identity,
    supervisor::resolve_executable,
};
use std::ffi::OsString;
use std::io::{Read, Write as _};
use std::path::Path;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::mpsc::{RecvTimeoutError, sync_channel};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const HOST_EVENT_RECORD_BYTES: usize = 64 * 1024;
const HOST_EVENT_WIRE_BYTES: usize = 192 * 1024 * 1024;
const HOST_DIAGNOSTIC_BYTES: usize = 4096;
const HOST_IDENTITY_BYTES: usize = 64 * 1024;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

#[derive(Debug)]
struct HostExecution {
    completion: WindowsHostCompletion,
    child_stderr: Vec<u8>,
    duration: Duration,
}

#[derive(Debug, Clone, Copy)]
struct HostDeadlines {
    startup: Option<Duration>,
    run: Duration,
    cancel_after_start: Option<Duration>,
    settlement: Duration,
}

#[allow(clippy::too_many_lines)]
pub(crate) fn supervise_windows(
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
    let build = compiled_windows_host_identity();
    if !build.admitted {
        return Err(SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "this runner was not compiled to admit the Windows process host",
        ));
    }
    let digest = build.sha256.as_deref().ok_or_else(|| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "the admitted Windows process host digest is unavailable",
        )
    })?;
    let wrapper_input = spec.wrapper_executable.as_deref().ok_or_else(|| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "the canonical prose wrapper path is required on Windows",
        )
    })?;
    let verified_host =
        verify_windows_host_sibling(wrapper_input, digest).map_err(supervisor_from_host_client)?;
    // The digest guard is held across every probe and child-host spawn.
    let _identity = probe_host_identity(
        &verified_host,
        &spec.cancellation,
        spec.startup_timeout.min(Duration::from_secs(5)),
    )?;

    let cwd = std::fs::canonicalize(&spec.cwd).map_err(|_| {
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
    let wrapper = std::fs::canonicalize(wrapper_input).map_err(|_| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "canonical prose wrapper path is unavailable",
        )
    })?;
    if wrapper == executable {
        return Err(SupervisorFailure::new(
            FailureKind::RecursiveInvocation,
            "selected harness resolves to the prose wrapper executable",
        ));
    }
    spec.environment = spec
        .environment
        .set(RECURSION_MARKER, &spec.recursion_token, Sensitivity::Secret)
        .set(RUN_NONCE, &spec.run_nonce, Sensitivity::Secret)
        .set(INVOCATION_ID, &spec.invocation_id, Sensitivity::Public);
    let secrets = spec.environment.secret_strings();

    let probed_version = match &spec.version_probe {
        Some(probe) => {
            let request = host_request(
                &spec,
                &executable,
                &wrapper,
                &cwd,
                &probe.argv,
                Vec::new(),
                probe.timeout,
                WindowsHostLimits {
                    max_stdout_bytes: probe.max_output_bytes,
                    max_stderr_bytes: probe.max_output_bytes,
                    max_queued_chunks: spec.limits.max_queued_records,
                },
            )?;
            let mut stdout = Vec::new();
            let execution = execute_host(
                &verified_host,
                &request,
                &spec,
                HostDeadlines {
                    startup: None,
                    run: probe.timeout,
                    cancel_after_start: None,
                    settlement: settlement_timeout(spec.termination_grace),
                },
                &secrets,
                |bytes| {
                    stdout.extend_from_slice(bytes);
                    Ok(false)
                },
            )?;
            let selected_output = match probe.output {
                VersionProbeOutput::Stdout => &stdout,
                VersionProbeOutput::Stderr => &execution.child_stderr,
            };
            let version = validate_windows_version_probe(
                &execution.completion,
                selected_output,
                probe.max_output_bytes,
            )
            .map_err(supervisor_from_host_client)?;
            if probe
                .required_substring
                .as_ref()
                .is_some_and(|required| !version.contains(required))
            {
                return Err(SupervisorFailure::new(
                    FailureKind::HarnessIncompatible,
                    "harness version response is outside the adapter admission range",
                ));
            }
            Some(version)
        }
        None => None,
    };

    let argv = spec.argv.clone();
    let stdin = spec.stdin.take().unwrap_or_default();
    let request = host_request(
        &spec,
        &executable,
        &wrapper,
        &cwd,
        &argv,
        stdin,
        spec.run_timeout,
        WindowsHostLimits {
            max_stdout_bytes: spec.limits.max_stdout_bytes,
            max_stderr_bytes: spec.limits.max_stderr_bytes,
            max_queued_chunks: spec.limits.max_queued_records,
        },
    )?;
    let mut state = ProtocolState::default();
    let mut assembler = HarnessJsonlAssembler::new(spec.limits.max_record_bytes);
    let execution = execute_host(
        &verified_host,
        &request,
        &spec,
        HostDeadlines {
            startup: Some(spec.startup_timeout),
            run: spec.run_timeout,
            cancel_after_start: spec.cancel_after_start,
            settlement: settlement_timeout(spec.termination_grace),
        },
        &secrets,
        |bytes| {
            let before = state.records.len();
            assembler.push(bytes, &mut state, protocol)?;
            if let Some(observer) = observer.as_deref_mut() {
                for record in &state.records[before..] {
                    observer.observe(record)?;
                }
            }
            Ok(state.started)
        },
    )
    .map_err(|failure| decorate_failure(failure, &state))?;
    assembler
        .finish()
        .map_err(|failure| decorate_failure(failure, &state))?;
    let diagnostic = sanitize_diagnostic(&execution.child_stderr, &secrets);
    let process_exit = native_exit_i32(execution.completion.process_exit_code);
    let Some(terminal_envelope) = state.terminal else {
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::ProtocolTruncated,
            message: "harness stream ended without its required terminal record".to_owned(),
            stderr: diagnostic,
            process_exit: Some(process_exit),
            process_signal: None,
            process_started: true,
            terminal_observed: false,
            terminal_envelope: None,
            records: state.records,
        });
    };
    if process_exit != 0 {
        return Err(SupervisorFailure {
            transport_diagnostic: None,
            kind: FailureKind::HarnessFailed,
            message: "harness exited unsuccessfully after a terminal record".to_owned(),
            stderr: diagnostic,
            process_exit: Some(process_exit),
            process_signal: None,
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
        process_exit,
        process_signal: None,
        containment: crate::ContainmentClaim::WindowsNativeProcessHost,
        probed_version,
        duration: execution.duration,
    })
}

#[allow(clippy::too_many_arguments)]
fn host_request(
    spec: &ProcessSpec,
    executable: &Path,
    wrapper: &Path,
    cwd: &Path,
    argv: &[OsString],
    child_stdin: Vec<u8>,
    run_timeout: Duration,
    limits: WindowsHostLimits,
) -> Result<WindowsHostRequest, SupervisorFailure> {
    let text = |value: &std::ffi::OsStr| {
        value.to_str().map(ToOwned::to_owned).ok_or_else(|| {
            SupervisorFailure::new(
                FailureKind::ContainmentUnsupported,
                "Windows process host inputs must be valid Unicode",
            )
        })
    };
    let environment = spec
        .environment
        .entries()
        .map(|(name, value)| {
            Ok(WindowsHostEnvironmentEntry {
                name: text(name)?,
                value: text(value)?,
            })
        })
        .collect::<Result<Vec<_>, SupervisorFailure>>()?;
    let request = WindowsHostRequest {
        request_id: spec.invocation_id.clone(),
        executable: text(executable.as_os_str())?,
        wrapper_executable: text(wrapper.as_os_str())?,
        argv: argv
            .iter()
            .map(|argument| text(argument))
            .collect::<Result<Vec<_>, _>>()?,
        cwd: text(cwd.as_os_str())?,
        environment,
        child_stdin,
        limits,
        cancellation: WindowsHostCancellation {
            graceful: WindowsGracefulControl::CtrlBreak,
            grace_ms: duration_millis(spec.termination_grace)?,
            hard_kill_after_ms: duration_millis(
                spec.termination_grace.max(Duration::from_millis(1)),
            )?,
        },
        run_timeout_ms: duration_millis(run_timeout)?,
    };
    // Validate and size the request before any helper spawn.
    encode_windows_host_request(&request).map_err(supervisor_from_host_request)?;
    Ok(request)
}

fn duration_millis(duration: Duration) -> Result<u64, SupervisorFailure> {
    u64::try_from(duration.as_millis()).map_err(|_| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "Windows process host duration exceeds its closed bound",
        )
    })
}

fn settlement_timeout(grace: Duration) -> Duration {
    grace
        .saturating_mul(2)
        .saturating_add(Duration::from_secs(2))
}

#[allow(clippy::too_many_lines)]
fn execute_host(
    host: &VerifiedWindowsHost,
    request: &WindowsHostRequest,
    spec: &ProcessSpec,
    deadlines: HostDeadlines,
    secrets: &[String],
    mut stdout_sink: impl FnMut(&[u8]) -> Result<bool, SupervisorFailure>,
) -> Result<HostExecution, SupervisorFailure> {
    let request_line = encode_windows_host_request(request).map_err(supervisor_from_host_client)?;
    let cancel_line =
        encode_windows_cancel_control("caller:cancel").map_err(supervisor_from_host_client)?;
    let mut client = WindowsHostClient::new(request.request_id.clone(), request.limits)
        .map_err(supervisor_from_host_client)?;
    let mut command = helper_command(host.path());
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|_| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "verified Windows process host could not be started",
        )
    })?;
    let Some(mut stdin) = child.stdin.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SupervisorFailure::new(
            FailureKind::Internal,
            "Windows process host control pipe is unavailable",
        ));
    };
    if stdin.write_all(&request_line).is_err() || stdin.flush().is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "Windows process host rejected its bounded request channel",
        ));
    }
    let Some(stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SupervisorFailure::new(
            FailureKind::Internal,
            "Windows process host event pipe is unavailable",
        ));
    };
    let Some(stderr) = child.stderr.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SupervisorFailure::new(
            FailureKind::Internal,
            "Windows process host diagnostic pipe is unavailable",
        ));
    };
    let host_limits = StreamLimits {
        max_record_bytes: HOST_EVENT_RECORD_BYTES,
        max_stdout_bytes: HOST_EVENT_WIRE_BYTES,
        max_stderr_bytes: HOST_DIAGNOSTIC_BYTES,
        max_queued_records: request.limits.max_queued_chunks,
    };
    let (sender, receiver) = sync_channel(host_limits.max_queued_records.max(1));
    let stdout_sender = sender.clone();
    let stdout_reader = thread::spawn(move || read_jsonl(stdout, host_limits, &stdout_sender));
    let stderr_sender = sender.clone();
    let stderr_reader = thread::spawn(move || read_stderr(stderr, host_limits, &stderr_sender));
    drop(sender);

    let started_at = Instant::now();
    let mut child_stderr = Vec::new();
    let mut host_stderr = Vec::new();
    let mut host_started = false;
    let mut harness_started = false;
    let mut stdout_eof = false;
    let mut stderr_eof = false;
    let mut status = None;
    let mut pending = None;
    let mut cancel_sent = false;
    let mut cancel_after_deadline = None;
    let mut settlement_deadline = None;

    loop {
        if status.is_none() {
            match child.try_wait() {
                Ok(observed) => status = observed,
                Err(_) if pending.is_none() => {
                    pending = Some(SupervisorFailure::new(
                        FailureKind::Internal,
                        "cannot inspect Windows process host",
                    ));
                }
                Err(_) => {}
            }
        }
        let elapsed = started_at.elapsed();
        if pending.is_none() && spec.cancellation.is_cancelled() {
            pending = Some(SupervisorFailure::new(
                FailureKind::Cancelled,
                "run was cancelled before harness completion",
            ));
        }
        if pending.is_none()
            && deadlines
                .startup
                .is_some_and(|deadline| !harness_started && elapsed >= deadline)
        {
            pending = Some(SupervisorFailure::new(
                FailureKind::StartupTimeout,
                "harness did not emit its startup record before the deadline",
            ));
        }
        if pending.is_none() && elapsed >= deadlines.run {
            pending = Some(SupervisorFailure::new(
                FailureKind::RunTimeout,
                "harness run exceeded its configured deadline",
            ));
        }
        if pending.is_none()
            && cancel_after_deadline.is_some_and(|deadline| Instant::now() >= deadline)
        {
            pending = Some(SupervisorFailure::new(
                FailureKind::Cancelled,
                "run was cancelled before harness completion",
            ));
        }
        if pending.is_some() && !cancel_sent {
            cancel_sent = true;
            let _ = stdin.write_all(&cancel_line).and_then(|()| stdin.flush());
            settlement_deadline = Some(Instant::now() + deadlines.settlement);
        }

        match receiver.recv_timeout(Duration::from_millis(5)) {
            Ok(ReaderMessage::Record(line)) => match client.accept_line(&line) {
                Ok(Some(WindowsHostChunk::Started { .. })) => host_started = true,
                Ok(Some(WindowsHostChunk::Stdout(bytes))) => match stdout_sink(&bytes) {
                    Ok(started) => {
                        if started && !harness_started {
                            harness_started = true;
                            if let Some(delay) = deadlines.cancel_after_start {
                                cancel_after_deadline = Some(Instant::now() + delay);
                            }
                        }
                    }
                    Err(error) if pending.is_none() => pending = Some(error),
                    Err(_) => {}
                },
                Ok(Some(WindowsHostChunk::Stderr(bytes))) => child_stderr.extend_from_slice(&bytes),
                Ok(None) => {}
                Err(error) if pending.is_none() => {
                    pending = Some(supervisor_from_host_client(error));
                }
                Err(_) => {}
            },
            Ok(ReaderMessage::StdoutEof) => stdout_eof = true,
            Ok(ReaderMessage::StdoutTruncated { .. } | ReaderMessage::StdoutIo)
                if pending.is_none() =>
            {
                pending = Some(SupervisorFailure::new(
                    FailureKind::ProtocolTruncated,
                    "Windows process host event channel ended incompletely",
                ));
            }
            Ok(ReaderMessage::StdoutLimit { .. }) if pending.is_none() => {
                pending = Some(SupervisorFailure::new(
                    FailureKind::ProtocolMalformed,
                    "Windows process host event channel exceeded a fixed bound",
                ));
            }
            Ok(ReaderMessage::Stderr(bytes)) => host_stderr.extend_from_slice(&bytes),
            Ok(ReaderMessage::StderrEof) => stderr_eof = true,
            Ok(ReaderMessage::StderrLimit | ReaderMessage::StderrIo) if pending.is_none() => {
                pending = Some(SupervisorFailure::new(
                    FailureKind::ProtocolMalformed,
                    "Windows process host diagnostic channel was invalid",
                ));
            }
            Ok(_) => {}
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                stdout_eof = true;
                stderr_eof = true;
            }
        }
        if status.is_some() && stdout_eof && stderr_eof {
            break;
        }
        if settlement_deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            let _ = child.kill();
            let _ = child.wait();
            drop(stdin);
            drop(receiver);
            join_reader(stdout_reader);
            join_reader(stderr_reader);
            return Err(decorate_host_failure(
                SupervisorFailure::new(
                    FailureKind::CleanupFailed,
                    "Windows process host did not provide authoritative cleanup evidence",
                ),
                host_started,
                client.terminal_completion(),
                &child_stderr,
                secrets,
            ));
        }
    }
    drop(stdin);
    drop(receiver);
    join_reader(stdout_reader);
    join_reader(stderr_reader);
    let status = match status {
        Some(status) => status,
        None => child.wait().map_err(|_| {
            SupervisorFailure::new(
                FailureKind::Internal,
                "Windows process host could not be reaped",
            )
        })?,
    };
    let terminal = client.terminal_completion().cloned();
    let poisoned = client.is_poisoned();
    let completion = client.finish(status.code());

    if poisoned {
        if terminal.as_ref().is_some_and(|terminal| {
            terminal.cleanup_verified
                && terminal.active_processes_after_cleanup == 0
                && status.success()
        }) {
            return Err(decorate_host_failure(
                pending.unwrap_or_else(|| {
                    SupervisorFailure::new(
                        FailureKind::ProtocolMalformed,
                        "Windows process host emitted an invalid lifecycle record",
                    )
                }),
                host_started,
                terminal.as_ref(),
                &child_stderr,
                secrets,
            ));
        }
        return Err(decorate_host_failure(
            SupervisorFailure::new(
                FailureKind::CleanupFailed,
                "Windows process host cleanup evidence could not be authenticated",
            ),
            host_started,
            terminal.as_ref(),
            &child_stderr,
            secrets,
        ));
    }
    if terminal.as_ref().is_some_and(|terminal| {
        !terminal.cleanup_verified || terminal.active_processes_after_cleanup != 0
    }) {
        return Err(decorate_host_failure(
            SupervisorFailure::new(
                FailureKind::CleanupFailed,
                "Windows process host did not prove its Job empty",
            ),
            host_started,
            terminal.as_ref(),
            &child_stderr,
            secrets,
        ));
    }
    if !host_stderr.is_empty() {
        if terminal.is_none() {
            if let Ok(bootstrap) =
                parse_windows_host_bootstrap_error(&host_stderr, HOST_DIAGNOSTIC_BYTES)
            {
                return Err(decorate_host_failure(
                    SupervisorFailure::new(
                        bootstrap.supervisor_kind(),
                        "Windows process host failed before authoritative completion",
                    ),
                    host_started,
                    None,
                    &child_stderr,
                    secrets,
                ));
            }
            return Err(decorate_host_failure(
                SupervisorFailure::new(
                    FailureKind::CleanupFailed,
                    "Windows process host ended without authoritative cleanup evidence",
                ),
                host_started,
                None,
                &child_stderr,
                secrets,
            ));
        }
        return Err(decorate_host_failure(
            SupervisorFailure::new(
                FailureKind::ProtocolMalformed,
                "Windows process host emitted an unexpected diagnostic",
            ),
            host_started,
            terminal.as_ref(),
            &child_stderr,
            secrets,
        ));
    }
    if terminal.is_none() {
        return Err(decorate_host_failure(
            SupervisorFailure::new(
                FailureKind::CleanupFailed,
                "Windows process host ended without authoritative cleanup evidence",
            ),
            host_started,
            None,
            &child_stderr,
            secrets,
        ));
    }
    match completion {
        Ok(completion) => {
            if let Some(failure) = pending {
                return Err(decorate_host_failure(
                    failure,
                    host_started,
                    Some(&completion),
                    &child_stderr,
                    secrets,
                ));
            }
            Ok(HostExecution {
                completion,
                child_stderr,
                duration: started_at.elapsed(),
            })
        }
        Err(error) => {
            if let (true, Some(failure)) = (
                matches!(
                    error.kind,
                    WindowsHostClientFailureKind::Cancelled
                        | WindowsHostClientFailureKind::TimedOut
                ),
                pending,
            ) {
                return Err(decorate_host_failure(
                    failure,
                    host_started,
                    terminal.as_ref(),
                    &child_stderr,
                    secrets,
                ));
            }
            Err(decorate_host_failure(
                supervisor_from_host_client(error),
                host_started,
                terminal.as_ref(),
                &child_stderr,
                secrets,
            ))
        }
    }
}

fn probe_host_identity(
    host: &VerifiedWindowsHost,
    cancellation: &crate::CancellationToken,
    timeout: Duration,
) -> Result<crate::WindowsHostIdentity, SupervisorFailure> {
    let output = run_bounded_probe(
        host,
        "--identity-json",
        cancellation,
        timeout,
        HOST_IDENTITY_BYTES,
    )?;
    if !output.stderr.is_empty() || !output.status.success() {
        return Err(SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "verified Windows process host identity probe failed",
        ));
    }
    validate_windows_host_identity(&output.stdout, HOST_IDENTITY_BYTES)
        .map_err(supervisor_from_host_client)
}

#[derive(Debug)]
struct BoundedProbeOutput {
    status: ExitStatus,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

#[allow(clippy::too_many_lines)]
fn run_bounded_probe(
    host: &VerifiedWindowsHost,
    argument: &str,
    cancellation: &crate::CancellationToken,
    timeout: Duration,
    maximum: usize,
) -> Result<BoundedProbeOutput, SupervisorFailure> {
    let mut command = helper_command(host.path());
    command
        .arg(argument)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|_| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "verified Windows process host probe could not be started",
        )
    })?;
    let Some(stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SupervisorFailure::new(
            FailureKind::Internal,
            "host probe output pipe is unavailable",
        ));
    };
    let Some(stderr) = child.stderr.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(SupervisorFailure::new(
            FailureKind::Internal,
            "host probe diagnostic pipe is unavailable",
        ));
    };
    let stdout_reader = bounded_reader(stdout, maximum);
    let stderr_reader = bounded_reader(stderr, HOST_DIAGNOSTIC_BYTES);
    let deadline = Instant::now() + timeout;
    let status = loop {
        if cancellation.is_cancelled() {
            let _ = child.kill();
            let _ = child.wait();
            let _ = join_bounded_reader(stdout_reader, maximum);
            let _ = join_bounded_reader(stderr_reader, HOST_DIAGNOSTIC_BYTES);
            return Err(SupervisorFailure::new(
                FailureKind::Cancelled,
                "run was cancelled during Windows process host probe",
            ));
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = join_bounded_reader(stdout_reader, maximum);
                let _ = join_bounded_reader(stderr_reader, HOST_DIAGNOSTIC_BYTES);
                return Err(SupervisorFailure::new(
                    FailureKind::ContainmentUnsupported,
                    "cannot inspect Windows process host probe",
                ));
            }
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = join_bounded_reader(stdout_reader, maximum);
            let _ = join_bounded_reader(stderr_reader, HOST_DIAGNOSTIC_BYTES);
            return Err(SupervisorFailure::new(
                FailureKind::StartupTimeout,
                "Windows process host probe exceeded its deadline",
            ));
        }
        thread::sleep(Duration::from_millis(5));
    };
    let stdout = join_bounded_reader(stdout_reader, maximum);
    let stderr = join_bounded_reader(stderr_reader, HOST_DIAGNOSTIC_BYTES);
    Ok(BoundedProbeOutput {
        status,
        stdout: stdout?,
        stderr: stderr?,
    })
}

fn helper_command(path: &Path) -> Command {
    use std::os::windows::process::CommandExt as _;

    let mut command = Command::new(path);
    command.env_clear().creation_flags(CREATE_NEW_PROCESS_GROUP);
    command
}

fn bounded_reader(
    reader: impl Read + Send + 'static,
    maximum: usize,
) -> JoinHandle<std::io::Result<Vec<u8>>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        reader
            .take(u64::try_from(maximum.saturating_add(1)).unwrap_or(u64::MAX))
            .read_to_end(&mut bytes)?;
        Ok(bytes)
    })
}

fn join_bounded_reader(
    reader: JoinHandle<std::io::Result<Vec<u8>>>,
    maximum: usize,
) -> Result<Vec<u8>, SupervisorFailure> {
    let bytes = reader.join().ok().and_then(Result::ok).ok_or_else(|| {
        SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "Windows process host probe output could not be read",
        )
    })?;
    if bytes.len() > maximum {
        return Err(SupervisorFailure::new(
            FailureKind::ContainmentUnsupported,
            "Windows process host probe output exceeded its bound",
        ));
    }
    Ok(bytes)
}

fn decorate_failure(mut failure: SupervisorFailure, state: &ProtocolState) -> SupervisorFailure {
    failure.terminal_observed = state.terminal.is_some();
    failure.terminal_envelope.clone_from(&state.terminal);
    failure.records.clone_from(&state.records);
    failure
}

fn decorate_host_failure(
    mut failure: SupervisorFailure,
    host_started: bool,
    terminal: Option<&WindowsHostCompletion>,
    child_stderr: &[u8],
    secrets: &[String],
) -> SupervisorFailure {
    failure.process_started = host_started;
    failure.stderr = sanitize_diagnostic(child_stderr, secrets);
    failure.process_exit = terminal.map(|completion| native_exit_i32(completion.process_exit_code));
    failure
}

fn supervisor_from_host_client(error: WindowsHostClientFailure) -> SupervisorFailure {
    SupervisorFailure::new(error.supervisor_kind(), error.to_string())
}

fn supervisor_from_host_request(error: WindowsHostClientFailure) -> SupervisorFailure {
    let kind = match error.kind {
        WindowsHostClientFailureKind::RequestInvalid
        | WindowsHostClientFailureKind::RequestLimit => FailureKind::ContainmentUnsupported,
        _ => error.supervisor_kind(),
    };
    SupervisorFailure::new(kind, error.to_string())
}

fn native_exit_i32(exit: u32) -> i32 {
    i32::from_ne_bytes(exit.to_ne_bytes())
}

fn join_reader(reader: JoinHandle<()>) {
    let _ = reader.join();
}
