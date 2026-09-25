#![cfg(unix)]

use prose_process_supervisor::{
    CancellationToken, CommandProbe, ContainmentClaim, EnvironmentPolicy, FailureKind,
    JsonlProtocol, PrivatePromptFiles, ProcessSpec, RUN_NONCE, Sensitivity, StdinLifecycle,
    StreamLimits, VersionProbe, VersionProbeOutput, probe_command, probe_version,
    strict_containment_available, supervise, supervise_observed,
};
use serde_json::Value;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};
use tempfile::TempDir;

const IMAGE: &[u8] = b"opaque image\0bytes\n\xce\xbb\n";
const TASK: &[u8] = b"{\"schema\":\"openprose.task-envelope/1\",\"argv\":[\"prose\",\"run\",\"two words\",\";$(touch nope)\"],\"interactionMode\":\"non-interactive\"}\n";

struct EscapedProcess {
    pid: rustix::process::Pid,
    process_group: rustix::process::Pid,
}

impl Drop for EscapedProcess {
    fn drop(&mut self) {
        use rustix::process::{Signal, getpgid, getpgrp, kill_process, test_kill_process};

        if self.process_group == self.pid
            && self.process_group != getpgrp()
            && getpgid(Some(self.pid)) == Ok(self.process_group)
        {
            let _ = kill_process(self.pid, Signal::KILL);
            let deadline = Instant::now() + Duration::from_secs(2);
            while test_kill_process(self.pid).is_ok() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

fn fake_harness() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../conformance/fake-harness/fake_harness.py")
        .canonicalize()
        .unwrap()
}

fn python() -> PathBuf {
    let output = Command::new("python3")
        .args(["-c", "import sys; print(sys.executable)"])
        .output()
        .expect("python3 must be available for the independently owned fixture");
    assert!(output.status.success());
    PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
        .canonicalize()
        .unwrap()
}

fn base_spec(
    root: &Path,
    prompts: &PrivatePromptFiles,
    scenario: &str,
    observation: &Path,
) -> ProcessSpec {
    ProcessSpec {
        executable: python(),
        argv: vec![
            fake_harness().into_os_string(),
            "run".into(),
            "--scenario".into(),
            scenario.into(),
            "--image-file".into(),
            prompts.image_path().into(),
            "--task-file".into(),
            prompts.task_path().into(),
            "--observation-file".into(),
            observation.into(),
        ],
        cwd: root.to_owned(),
        wrapper_executable: std::env::current_exe().ok(),
        version_probe: None,
        stdin: None,
        stdin_lifecycle: StdinLifecycle::CloseAfterWrite,
        environment: EnvironmentPolicy::from_current()
            .allow_inherited("PATH", Sensitivity::Public)
            .allow_inherited("LANG", Sensitivity::Public),
        invocation_id: "fixture-invocation-0001".to_owned(),
        recursion_token: "fixture-recursion-0001".to_owned(),
        run_nonce: "fixture-nonce-0001".to_owned(),
        startup_timeout: Duration::from_secs(2),
        run_timeout: Duration::from_secs(3),
        termination_grace: Duration::from_millis(100),
        limits: StreamLimits::default(),
        cancellation: CancellationToken::default(),
        cancel_after_start: None,
    }
}

fn run_scenario(scenario: &str) -> Result<prose_process_supervisor::ProcessOutcome, FailureKind> {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("observation.json");
    let result = supervise(
        base_spec(root.path(), &prompts, scenario, &observation),
        &JsonlProtocol::fake_harness(),
    );
    result.map_err(|error| error.kind)
}

#[test]
fn success_delivers_exact_files_cwd_metadata_and_structured_terminal() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation_path = root.path().join("observation.json");
    let mut spec = base_spec(root.path(), &prompts, "success", &observation_path);
    spec.version_probe = Some(VersionProbe {
        argv: vec![fake_harness().into_os_string(), "--version".into()],
        timeout: Duration::from_secs(1),
        max_output_bytes: 1024,
        required_substring: Some("1.0.0".to_owned()),
        output: VersionProbeOutput::Stdout,
    });
    let outcome = supervise(spec, &JsonlProtocol::fake_harness()).unwrap();
    assert_eq!(outcome.process_exit, 0);
    assert_eq!(outcome.records.len(), 3);
    assert_eq!(
        outcome.terminal_envelope["semanticStatus"],
        Value::String("not-applicable".to_owned())
    );
    assert!(outcome.stderr.is_empty());
    assert!(outcome.probed_version.unwrap().contains("1.0.0"));
    let observation: Value = serde_json::from_slice(&fs::read(observation_path).unwrap()).unwrap();
    assert_eq!(
        observation["cwd"],
        root.path().canonicalize().unwrap().to_str().unwrap()
    );
    assert_eq!(observation["image"]["byteLength"], IMAGE.len());
    assert_eq!(observation["task"]["byteLength"], TASK.len());
    assert_eq!(
        observation["environment"],
        serde_json::json!({
            "OPENPROSE_INVOCATION_ID":"fixture-invocation-0001",
            "OPENPROSE_RECURSION_TOKEN":"fixture-recursion-0001",
            "OPENPROSE_RUN_NONCE":"fixture-nonce-0001"
        })
    );
}

#[test]
fn observer_failure_terminates_and_settles_the_owned_process() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation_path = root.path().join("observation.json");
    let mut observer = |_record: &Value| {
        Err(prose_process_supervisor::SupervisorFailure {
            kind: FailureKind::Internal,
            message: "fixture observer failure".to_owned(),
            transport_diagnostic: None,
            stderr: String::new(),
            process_exit: None,
            process_signal: None,
            process_started: false,
            terminal_observed: false,
            terminal_envelope: None,
            records: Vec::new(),
        })
    };
    let error = supervise_observed(
        base_spec(root.path(), &prompts, "success", &observation_path),
        &JsonlProtocol::fake_harness(),
        &mut observer,
    )
    .unwrap_err();
    assert_eq!(error.kind, FailureKind::Internal);
    assert!(error.process_started);
    assert_eq!(error.records.len(), 1);
}

#[test]
fn version_probe_reads_only_the_adapter_selected_stream() {
    let root = TempDir::new().unwrap();
    let environment = EnvironmentPolicy::from_current()
        .allow_inherited("PATH", Sensitivity::Public)
        .allow_inherited("LANG", Sensitivity::Public);
    let mut probe = VersionProbe {
        argv: vec![
            "-c".into(),
            "import sys; sys.stdout.write('wrong 9.9.9\\n'); sys.stderr.write('0.7.0\\n')".into(),
        ],
        timeout: Duration::from_secs(1),
        max_output_bytes: 1024,
        required_substring: None,
        output: VersionProbeOutput::Stderr,
    };
    assert_eq!(
        probe_version(
            &python(),
            root.path(),
            &environment,
            &probe,
            &CancellationToken::default(),
        )
        .unwrap(),
        "0.7.0"
    );

    probe.output = VersionProbeOutput::Stdout;
    assert_eq!(
        probe_version(
            &python(),
            root.path(),
            &environment,
            &probe,
            &CancellationToken::default(),
        )
        .unwrap(),
        "wrong 9.9.9"
    );
}

#[test]
fn version_probe_fails_bounded_and_cleans_a_descendant_retaining_its_pipe() {
    use rustix::process::{Pid, test_kill_process_group};

    let root = TempDir::new().unwrap();
    let identities_path = root.path().join("version-probe-descendant.json");
    let program = r"
import json, os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(30)
    os._exit(0)
with open(sys.argv[1], 'x', encoding='utf-8') as target:
    json.dump({'pid':pid,'pgid':os.getpgrp()}, target)
print('0.7.0', flush=True)
os._exit(0)
";
    let probe = VersionProbe {
        argv: vec![
            "-c".into(),
            program.into(),
            identities_path.clone().into_os_string(),
        ],
        timeout: Duration::from_secs(1),
        max_output_bytes: 1024,
        required_substring: None,
        output: VersionProbeOutput::Stdout,
    };
    let started = Instant::now();
    let error = probe_version(
        &python(),
        root.path(),
        &EnvironmentPolicy::default(),
        &probe,
        &CancellationToken::default(),
    )
    .unwrap_err();
    assert_eq!(error.kind, FailureKind::CleanupFailed);
    assert!(started.elapsed() < Duration::from_secs(4));
    let identity: Value = serde_json::from_slice(&fs::read(identities_path).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    assert!(test_kill_process_group(process_group).is_err());
}

#[test]
fn version_probe_does_not_hang_on_an_escaped_descendant_retaining_its_pipe() {
    use rustix::process::Pid;

    let root = TempDir::new().unwrap();
    let identity_path = root.path().join("escaped-version-probe-descendant.json");
    let program = r"
import json, os, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    with open(sys.argv[1], 'x', encoding='utf-8') as target:
        json.dump({'pid':os.getpid(),'pgid':os.getpgrp()}, target)
    time.sleep(30)
    os._exit(0)
deadline = time.monotonic() + 2
while not os.path.exists(sys.argv[1]):
    if time.monotonic() >= deadline:
        raise RuntimeError('escaped identity timeout')
    time.sleep(0.01)
print('0.7.0', flush=True)
os._exit(0)
";
    let probe = VersionProbe {
        argv: vec![
            "-c".into(),
            program.into(),
            identity_path.clone().into_os_string(),
        ],
        timeout: Duration::from_secs(1),
        max_output_bytes: 1024,
        required_substring: None,
        output: VersionProbeOutput::Stdout,
    };
    let started = Instant::now();
    let error = probe_version(
        &python(),
        root.path(),
        &EnvironmentPolicy::default(),
        &probe,
        &CancellationToken::default(),
    )
    .unwrap_err();
    let identity: Value = serde_json::from_slice(&fs::read(identity_path).unwrap()).unwrap();
    let pid = Pid::from_raw(i32::try_from(identity["pid"].as_i64().unwrap()).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    let _cleanup = EscapedProcess { pid, process_group };
    assert_eq!(error.kind, FailureKind::CleanupFailed);
    assert!(started.elapsed() < Duration::from_secs(4));
}

#[test]
fn readiness_probe_timeout_cleans_descendants_and_settles_both_readers() {
    use rustix::process::{Pid, test_kill_process_group};

    let root = TempDir::new().unwrap();
    let identities_path = root.path().join("readiness-probe-descendant.json");
    let program = r"
import json, os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(30)
    os._exit(0)
scratch = sys.argv[1] + '.tmp'
with open(scratch, 'x', encoding='utf-8') as target:
    json.dump({'pid':pid,'pgid':os.getpgrp()}, target)
    target.flush()
    os.fsync(target.fileno())
os.replace(scratch, sys.argv[1])
print('probe still running', flush=True)
print('diagnostic still running', file=sys.stderr, flush=True)
time.sleep(30)
";
    let probe = CommandProbe {
        argv: vec![
            "-c".into(),
            program.into(),
            identities_path.clone().into_os_string(),
        ],
        timeout: Duration::from_millis(100),
        max_output_bytes: 1024,
    };
    let started = Instant::now();
    let error = probe_command(
        &python(),
        root.path(),
        &EnvironmentPolicy::default(),
        &probe,
        &CancellationToken::default(),
    )
    .unwrap_err();
    assert_eq!(error.kind, FailureKind::StartupTimeout);
    assert!(started.elapsed() < Duration::from_secs(4));
    let identity: Value = serde_json::from_slice(&fs::read(identities_path).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    assert!(test_kill_process_group(process_group).is_err());
}

#[test]
fn readiness_probe_cancellation_cleans_descendants_and_settles_both_readers() {
    use rustix::process::{Pid, test_kill_process_group};

    let root = TempDir::new().unwrap();
    let identities_path = root
        .path()
        .join("cancelled-readiness-probe-descendant.json");
    let program = r"
import json, os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(30)
    os._exit(0)
scratch = sys.argv[1] + '.tmp'
with open(scratch, 'x', encoding='utf-8') as target:
    json.dump({'pid':pid,'pgid':os.getpgrp()}, target)
    target.flush()
    os.fsync(target.fileno())
os.replace(scratch, sys.argv[1])
print('probe still running', flush=True)
print('diagnostic still running', file=sys.stderr, flush=True)
time.sleep(30)
";
    let probe = CommandProbe {
        argv: vec![
            "-c".into(),
            program.into(),
            identities_path.clone().into_os_string(),
        ],
        timeout: Duration::from_secs(5),
        max_output_bytes: 1024,
    };
    let cancellation = CancellationToken::default();
    let scheduled = cancellation.clone();
    let watched_path = identities_path.clone();
    let canceller = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        while !watched_path.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            watched_path.exists(),
            "readiness fixture did not publish its identity"
        );
        scheduled.cancel();
    });
    let started = Instant::now();
    let error = probe_command(
        &python(),
        root.path(),
        &EnvironmentPolicy::default(),
        &probe,
        &cancellation,
    )
    .unwrap_err();
    canceller.join().unwrap();
    assert_eq!(error.kind, FailureKind::Cancelled);
    assert!(started.elapsed() < Duration::from_secs(4));
    let identity: Value = serde_json::from_slice(&fs::read(identities_path).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    assert!(test_kill_process_group(process_group).is_err());
}

#[test]
fn unix_process_group_is_explicitly_best_effort_not_strict_containment() {
    assert!(!strict_containment_available());
    assert_eq!(
        run_scenario("success").unwrap().containment,
        ContainmentClaim::UnixProcessGroupBestEffort
    );
}

#[test]
fn detached_setsid_descendant_is_not_misreported_as_strictly_contained() {
    use rustix::process::{Pid, getpgid, test_kill_process};

    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("unused-observation.json");
    let identity_path = root.path().join("setsid-escape.json");
    let program = r"
import json, os, signal, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    for fd in (0, 1, 2):
        try: os.close(fd)
        except OSError: pass
    with open(sys.argv[1], 'x', encoding='utf-8') as target:
        json.dump({'nonce':'setsid-escape-v1','pid':os.getpid(),'pgid':os.getpgrp()}, target)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
    os._exit(0)
deadline = time.monotonic() + 2
while not os.path.exists(sys.argv[1]):
    if time.monotonic() >= deadline: raise RuntimeError('escape identity timeout')
    time.sleep(0.01)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)
";
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec![
        "-c".into(),
        program.into(),
        identity_path.clone().into_os_string(),
    ];
    spec.version_probe = None;
    let outcome = supervise(spec, &JsonlProtocol::fake_harness()).unwrap();
    let identity: Value = serde_json::from_slice(&fs::read(identity_path).unwrap()).unwrap();
    assert_eq!(identity["nonce"], "setsid-escape-v1");
    let pid = Pid::from_raw(i32::try_from(identity["pid"].as_i64().unwrap()).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    let _cleanup = EscapedProcess { pid, process_group };
    assert_eq!(pid, process_group);
    assert_eq!(getpgid(Some(pid)), Ok(process_group));
    assert!(test_kill_process(pid).is_ok());
    assert_eq!(
        outcome.containment,
        ContainmentClaim::UnixProcessGroupBestEffort
    );
}

#[test]
fn run_deadline_does_not_hang_on_an_escaped_descendant_retaining_stream_pipes() {
    use rustix::process::Pid;

    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("unused-observation.json");
    let identity_path = root.path().join("setsid-pipe-holder.json");
    let program = r"
import json, os, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    with open(sys.argv[1], 'x', encoding='utf-8') as target:
        json.dump({'nonce':'setsid-pipe-holder-v1','pid':os.getpid(),'pgid':os.getpgrp()}, target)
    time.sleep(30)
    os._exit(0)
deadline = time.monotonic() + 2
while not os.path.exists(sys.argv[1]):
    if time.monotonic() >= deadline: raise RuntimeError('escape identity timeout')
    time.sleep(0.01)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)
os._exit(0)
";
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec![
        "-c".into(),
        program.into(),
        identity_path.clone().into_os_string(),
    ];
    spec.version_probe = None;
    spec.run_timeout = Duration::from_millis(150);
    spec.termination_grace = Duration::from_millis(50);
    let started = Instant::now();
    let error = supervise(spec, &JsonlProtocol::fake_harness()).unwrap_err();
    let identity: Value = serde_json::from_slice(&fs::read(identity_path).unwrap()).unwrap();
    let pid = Pid::from_raw(i32::try_from(identity["pid"].as_i64().unwrap()).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    let _cleanup = EscapedProcess { pid, process_group };
    assert_eq!(error.kind, FailureKind::CleanupFailed);
    assert!(error.terminal_observed);
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn successful_stream_does_not_hang_on_an_escaped_descendant_retaining_stdin() {
    use rustix::process::Pid;

    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("unused-observation.json");
    let identity_path = root.path().join("setsid-stdin-holder.json");
    let program = r"
import json, os, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    for fd in (1, 2):
        try: os.close(fd)
        except OSError: pass
    with open(sys.argv[1], 'x', encoding='utf-8') as target:
        json.dump({'nonce':'setsid-stdin-holder-v1','pid':os.getpid(),'pgid':os.getpgrp()}, target)
    time.sleep(30)
    os._exit(0)
deadline = time.monotonic() + 2
while not os.path.exists(sys.argv[1]):
    if time.monotonic() >= deadline: raise RuntimeError('escape identity timeout')
    time.sleep(0.01)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)
os._exit(0)
";
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec![
        "-c".into(),
        program.into(),
        identity_path.clone().into_os_string(),
    ];
    spec.stdin = Some(vec![b'x'; 1024 * 1024]);
    spec.version_probe = None;
    spec.run_timeout = Duration::from_secs(1);
    spec.termination_grace = Duration::from_millis(50);
    let started = Instant::now();
    let error = supervise(spec, &JsonlProtocol::fake_harness()).unwrap_err();
    let identity: Value = serde_json::from_slice(&fs::read(identity_path).unwrap()).unwrap();
    let pid = Pid::from_raw(i32::try_from(identity["pid"].as_i64().unwrap()).unwrap()).unwrap();
    let process_group =
        Pid::from_raw(i32::try_from(identity["pgid"].as_i64().unwrap()).unwrap()).unwrap();
    let _cleanup = EscapedProcess { pid, process_group };
    assert_eq!(error.kind, FailureKind::CleanupFailed);
    assert!(error.terminal_observed);
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn terminal_event_closes_retained_stdin_only_after_the_harness_finishes() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("unused-observation.json");
    let program = r"
import json, select, sys
if sys.stdin.buffer.readline() != b'prompt\n':
    raise RuntimeError('prompt bytes differ')
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)
readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.15)
if readable and sys.stdin.buffer.read(1) == b'':
    raise RuntimeError('stdin closed before the protocol terminal')
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','sessionId':'fake-session-0001','terminalEnvelope':{'schema':'openprose.sentinel-terminal-envelope/1','semanticStatus':'not-applicable','marker':'OPENPROSE_SENTINEL_TERMINAL_V1'}}), flush=True)
if sys.stdin.buffer.read() != b'':
    raise RuntimeError('unexpected bytes after the prompt')
";
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec!["-c".into(), program.into()];
    spec.stdin = Some(b"prompt\n".to_vec());
    spec.stdin_lifecycle = StdinLifecycle::CloseAfterTerminalEvent;
    spec.run_timeout = Duration::from_secs(2);

    let outcome = supervise(spec, &JsonlProtocol::fake_harness()).unwrap();
    assert_eq!(outcome.process_exit, 0);
    assert_eq!(outcome.records.len(), 2);
}

#[test]
fn rpc_lifecycle_terminal_closes_stdin_before_a_late_correlated_ack() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("unused-observation.json");
    let program = r"
import json, select, sys
if sys.stdin.buffer.readline() != b'prompt\n':
    raise RuntimeError('prompt bytes differ')
print(json.dumps({'type':'ready'}), flush=True)
readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.15)
if readable and sys.stdin.buffer.read(1) == b'':
    raise RuntimeError('stdin closed before the lifecycle terminal')
print(json.dumps({'type':'agent_start'}), flush=True)
print(json.dumps({'type':'agent_end','isTerminal':True}), flush=True)
if sys.stdin.buffer.read() != b'':
    raise RuntimeError('unexpected bytes after the prompt')
print(json.dumps({'type':'response','id':'fixture-invocation-0001','command':'prompt','success':True}), flush=True)
";
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec!["-c".into(), program.into()];
    spec.stdin = Some(b"prompt\n".to_vec());
    spec.stdin_lifecycle = StdinLifecycle::CloseAfterTerminalEvent;
    spec.run_timeout = Duration::from_secs(2);
    let protocol = JsonlProtocol::installed("ready", "agent_end", ["agent_start", "response"])
        .with_allowed_after_terminal_events(["response"]);

    let outcome = supervise(spec, &protocol).unwrap();
    assert_eq!(outcome.process_exit, 0);
    assert_eq!(outcome.records.len(), 4);
    assert_eq!(outcome.records.last().unwrap()["type"], "response");
}

#[test]
fn retained_stdin_is_stopped_and_settled_on_run_timeout() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("unused-observation.json");
    let program = r"
import json, sys, time
sys.stdin.buffer.readline()
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started','sessionId':'fake-session-0001','harnessVersion':'1.0.0'}), flush=True)
time.sleep(30)
";
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec!["-c".into(), program.into()];
    spec.stdin = Some(b"prompt\n".to_vec());
    spec.stdin_lifecycle = StdinLifecycle::CloseAfterTerminalEvent;
    spec.run_timeout = Duration::from_millis(100);
    spec.termination_grace = Duration::from_millis(50);

    let started = Instant::now();
    let error = supervise(spec, &JsonlProtocol::fake_harness()).unwrap_err();
    assert_eq!(error.kind, FailureKind::RunTimeout);
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn fragmented_crlf_delay_and_stderr_scenarios_remain_distinct() {
    assert_eq!(run_scenario("fragmented").unwrap().records.len(), 3);
    assert_eq!(run_scenario("crlf").unwrap().records.len(), 3);
    assert_eq!(run_scenario("delay").unwrap().records.len(), 3);
    let stderr = run_scenario("stderr").unwrap();
    assert!(stderr.stderr.contains("stderr remains separate"));
    assert!(
        stderr
            .records
            .iter()
            .all(|record| !record.to_string().contains("stderr remains separate"))
    );
}

#[test]
fn malformed_truncated_duplicate_reordered_and_exit_rules_fail_closed() {
    for scenario in ["malformed", "duplicate-terminal", "reordered"] {
        assert_eq!(
            run_scenario(scenario).unwrap_err(),
            FailureKind::ProtocolMalformed
        );
    }
    for scenario in ["truncated", "eof-without-terminal", "nonzero"] {
        assert_eq!(
            run_scenario(scenario).unwrap_err(),
            FailureKind::ProtocolTruncated
        );
    }
    assert_eq!(
        run_scenario("terminal-nonzero").unwrap_err(),
        FailureKind::HarnessFailed
    );
}

#[test]
fn malformed_protocol_retains_diagnostics_that_arrive_while_readers_settle() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("observation.json");
    let program = r#"
import os, sys, time
sys.stdout.write('{"schema":"openprose.fake-harness-event/1",not-json}\n')
sys.stdout.flush()
child = os.fork()
if child == 0:
    os.setsid()
    time.sleep(0.05)
    sys.stderr.write('late bounded diagnostic')
    sys.stderr.flush()
    os._exit(0)
os._exit(0)
"#;
    let mut spec = base_spec(root.path(), &prompts, "success", &observation);
    spec.executable = python();
    spec.argv = vec!["-c".into(), program.into()];

    let error = supervise(spec, &JsonlProtocol::fake_harness()).unwrap_err();

    assert_eq!(error.kind, FailureKind::ProtocolMalformed);
    assert_eq!(error.stderr, "late bounded diagnostic");
}

#[test]
fn startup_and_run_deadlines_have_separate_classifications() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("observation.json");
    let mut startup = base_spec(root.path(), &prompts, "delay", &observation);
    startup.argv.extend(["--delay-ms".into(), "100".into()]);
    startup.startup_timeout = Duration::from_millis(15);
    assert_eq!(
        supervise(startup, &JsonlProtocol::fake_harness())
            .unwrap_err()
            .kind,
        FailureKind::StartupTimeout
    );

    let mut run = base_spec(root.path(), &prompts, "delay", &observation);
    run.argv.extend(["--delay-ms".into(), "100".into()]);
    run.startup_timeout = Duration::from_secs(1);
    run.run_timeout = Duration::from_millis(15);
    assert_eq!(
        supervise(run, &JsonlProtocol::fake_harness())
            .unwrap_err()
            .kind,
        FailureKind::RunTimeout
    );
}

#[test]
fn cancellation_kills_resistant_child_and_grandchild_in_owned_group() {
    use rustix::process::{Pid, test_kill_process_group};

    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("observation.json");
    let identities_path = root.path().join("descendants.json");
    let mut spec = base_spec(root.path(), &prompts, "descendant", &observation);
    spec.argv.extend([
        "--descendant-pid-file".into(),
        identities_path.clone().into_os_string(),
    ]);
    let cancellation = spec.cancellation.clone();
    let watched_path = identities_path.clone();
    let canceller = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        while !watched_path.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        cancellation.cancel();
    });
    let error = supervise(spec, &JsonlProtocol::fake_harness()).unwrap_err();
    canceller.join().unwrap();
    assert_eq!(error.kind, FailureKind::Cancelled);
    let identities: Value = serde_json::from_slice(&fs::read(identities_path).unwrap()).unwrap();
    assert_eq!(identities["runNonce"], "fixture-nonce-0001");
    let process_group = identities["processGroupId"].as_i64().unwrap();
    let process_group = Pid::from_raw(i32::try_from(process_group).unwrap()).unwrap();
    assert!(test_kill_process_group(process_group).is_err());
}

#[test]
fn recursion_marker_wrapper_realpath_and_cancel_before_spawn_are_rejected() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("observation.json");
    let mut recursive = base_spec(root.path(), &prompts, "success", &observation);
    recursive.environment =
        EnvironmentPolicy::from_pairs([("OPENPROSE_RECURSION_TOKEN", "already-running")]);
    assert_eq!(
        supervise(recursive, &JsonlProtocol::fake_harness())
            .unwrap_err()
            .kind,
        FailureKind::RecursiveInvocation
    );

    let mut wrapper = base_spec(root.path(), &prompts, "success", &observation);
    wrapper.wrapper_executable = Some(wrapper.executable.clone());
    assert_eq!(
        supervise(wrapper, &JsonlProtocol::fake_harness())
            .unwrap_err()
            .kind,
        FailureKind::RecursiveInvocation
    );

    let cancelled = base_spec(root.path(), &prompts, "success", &observation);
    cancelled.cancellation.cancel();
    assert_eq!(
        supervise(cancelled, &JsonlProtocol::fake_harness())
            .unwrap_err()
            .kind,
        FailureKind::Cancelled
    );
}

#[test]
fn oversized_records_shell_metacharacters_and_secret_diagnostics_are_bounded() {
    let root = TempDir::new().unwrap();
    let marker = root.path().join("must-not-exist");
    let program = r"
import json, os, sys
assert sys.argv[1] == ';touch'
assert sys.argv[2].endswith('must-not-exist')
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.started'}))
print(os.environ['FIXTURE_SECRET_LONG'], file=sys.stderr)
print(json.dumps({'schema':'openprose.fake-harness-event/1','type':'session.completed','terminalEnvelope':{}}))
";
    let spec = ProcessSpec {
        executable: python(),
        argv: vec![
            "-c".into(),
            program.into(),
            ";touch".into(),
            marker.clone().into_os_string(),
        ],
        cwd: root.path().to_owned(),
        wrapper_executable: None,
        version_probe: None,
        stdin: None,
        stdin_lifecycle: StdinLifecycle::CloseAfterWrite,
        environment: EnvironmentPolicy::from_pairs([
            ("FIXTURE_SECRET", "top-secret-value"),
            ("FIXTURE_SECRET_LONG", "top-secret-value/overlapping-suffix"),
            (
                "FIXTURE_SECRET_DUPLICATE",
                "top-secret-value/overlapping-suffix",
            ),
        ])
        .allow_inherited("FIXTURE_SECRET", Sensitivity::Secret)
        .allow_inherited("FIXTURE_SECRET_LONG", Sensitivity::Secret)
        .allow_inherited("FIXTURE_SECRET_DUPLICATE", Sensitivity::Secret),
        invocation_id: "fixture-invocation".to_owned(),
        recursion_token: "fixture-recursion".to_owned(),
        run_nonce: "fixture-nonce".to_owned(),
        startup_timeout: Duration::from_secs(1),
        run_timeout: Duration::from_secs(1),
        termination_grace: Duration::from_millis(50),
        limits: StreamLimits::default(),
        cancellation: CancellationToken::default(),
        cancel_after_start: None,
    };
    let outcome = supervise(spec, &JsonlProtocol::fake_harness()).unwrap();
    assert!(!marker.exists());
    assert_eq!(outcome.stderr.trim(), "[REDACTED]");

    let oversized_program = "import sys; sys.stdout.write('x' * 65 + '\\n')";
    let oversized = ProcessSpec {
        executable: python(),
        argv: vec!["-c".into(), oversized_program.into()],
        cwd: root.path().to_owned(),
        wrapper_executable: None,
        version_probe: None,
        stdin: None,
        stdin_lifecycle: StdinLifecycle::CloseAfterWrite,
        environment: EnvironmentPolicy::default(),
        invocation_id: "fixture-invocation-2".to_owned(),
        recursion_token: "fixture-recursion-2".to_owned(),
        run_nonce: "fixture-nonce-2".to_owned(),
        startup_timeout: Duration::from_secs(1),
        run_timeout: Duration::from_secs(1),
        termination_grace: Duration::from_millis(50),
        limits: StreamLimits {
            max_record_bytes: 64,
            ..StreamLimits::default()
        },
        cancellation: CancellationToken::default(),
        cancel_after_start: None,
    };
    assert_eq!(
        supervise(oversized, &JsonlProtocol::fake_harness())
            .unwrap_err()
            .kind,
        FailureKind::ProtocolMalformed
    );
}

#[test]
fn run_nonce_is_never_rendered_by_debug() {
    let root = TempDir::new().unwrap();
    let prompts = PrivatePromptFiles::create(IMAGE, TASK).unwrap();
    let observation = root.path().join("observation.json");
    let spec = base_spec(root.path(), &prompts, "success", &observation);
    let debug = format!("{spec:?}");
    assert!(!debug.contains("fixture-nonce-0001"));
    assert!(!debug.contains("fixture-recursion-0001"));
    assert!(debug.contains("[REDACTED]"));
    let _: Vec<OsString> = spec.argv;
    assert_eq!(RUN_NONCE, "OPENPROSE_RUN_NONCE");
}
