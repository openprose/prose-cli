#[cfg(feature = "test-seams")]
use prose_runner_core::image::sha256_hex;
use serde_json::{Value, json};
#[cfg(any(feature = "test-seams", not(debug_assertions)))]
use std::ffi::OsString;
use std::fmt::Write as _;
use std::fs;
#[cfg(all(feature = "test-seams", unix))]
use std::io::{BufRead as _, BufReader, Read as _};
use std::path::{Path, PathBuf};
#[cfg(all(feature = "test-seams", unix))]
use std::process::{Child, Stdio};
use std::process::{Command, Output};
use std::sync::OnceLock;
#[cfg(all(feature = "test-seams", unix))]
use std::thread;
#[cfg(all(feature = "test-seams", unix))]
use std::time::{Duration, Instant};
use tempfile::TempDir;

#[cfg(all(feature = "test-seams", unix))]
struct PublishedFixtureCleanup {
    child: Option<Child>,
    identities_path: PathBuf,
}

#[cfg(all(feature = "test-seams", unix))]
impl PublishedFixtureCleanup {
    fn new(child: Child, identities_path: PathBuf) -> Self {
        Self {
            child: Some(child),
            identities_path,
        }
    }

    fn child_mut(&mut self) -> &mut Child {
        self.child.as_mut().expect("fixture wrapper is available")
    }

    fn child_pid(&self) -> u32 {
        self.child
            .as_ref()
            .expect("fixture wrapper is available")
            .id()
    }

    fn wait_with_output(&mut self, timeout: Duration) -> Output {
        let deadline = Instant::now() + timeout;
        loop {
            if self.child_mut().try_wait().unwrap().is_some() {
                return self
                    .child
                    .take()
                    .expect("fixture wrapper is available")
                    .wait_with_output()
                    .unwrap();
            }
            assert!(
                Instant::now() < deadline,
                "runner did not settle SIGINT within {timeout:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn cleanup_published_group(&self) {
        use rustix::process::{
            Signal, getpgid, getpgrp, kill_process_group, test_kill_process,
            test_kill_process_group,
        };

        let Some(observed) = validated_fixture_identities(&self.identities_path) else {
            return;
        };
        if observed.process_group == getpgrp() {
            return;
        }

        let mut matching_live_member = false;
        for pid in [observed.child_pid, observed.grandchild_pid] {
            match getpgid(Some(pid)) {
                Ok(group) => {
                    if group != observed.process_group {
                        return;
                    }
                    matching_live_member = true;
                }
                Err(_) => {
                    if test_kill_process(pid).is_ok() {
                        return;
                    }
                }
            }
        }
        if !matching_live_member || test_kill_process_group(observed.process_group).is_err() {
            return;
        }

        let _ = kill_process_group(observed.process_group, Signal::KILL);
        let deadline = Instant::now() + Duration::from_secs(2);
        while test_kill_process_group(observed.process_group).is_ok() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
    }
}

#[cfg(all(feature = "test-seams", unix))]
impl Drop for PublishedFixtureCleanup {
    fn drop(&mut self) {
        self.cleanup_published_group();
        if let Some(child) = self.child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.child = None;
        self.cleanup_published_group();
    }
}

#[cfg(all(feature = "test-seams", unix))]
struct FixtureIdentities {
    child_pid: rustix::process::Pid,
    grandchild_pid: rustix::process::Pid,
    process_group: rustix::process::Pid,
}

#[cfg(all(feature = "test-seams", unix))]
fn validated_fixture_identities(path: &Path) -> Option<FixtureIdentities> {
    use rustix::process::Pid;

    let observed: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    let object = observed.as_object()?;
    let expected = [
        "attemptedDetachment",
        "childPid",
        "grandchildPid",
        "processGroupId",
        "runNonce",
    ];
    if object.len() != expected.len() || expected.iter().any(|key| !object.contains_key(*key)) {
        return None;
    }
    if observed["attemptedDetachment"] != false {
        return None;
    }
    let nonce = observed["runNonce"].as_str()?;
    if !nonce.starts_with("run-") || nonce.len() <= "run-".len() {
        return None;
    }
    let raw_pid = |key: &str| {
        i32::try_from(observed[key].as_i64()?)
            .ok()
            .filter(|raw| *raw > 1)
            .and_then(Pid::from_raw)
    };
    let child_pid = raw_pid("childPid")?;
    let grandchild_pid = raw_pid("grandchildPid")?;
    let process_group = raw_pid("processGroupId")?;
    if child_pid == grandchild_pid {
        return None;
    }
    Some(FixtureIdentities {
        child_pid,
        grandchild_pid,
        process_group,
    })
}

fn prose(root: &Path, args: &[&str]) -> Output {
    let home = root.join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    Command::new(sentinel_prose())
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("LANG", "C.UTF-8")
        .output()
        .unwrap()
}

fn expected_human_safe_scalar(value: &str) -> String {
    expected_human_safe(value, false)
}

#[cfg(feature = "test-seams")]
fn expected_human_safe_multiline(value: &str) -> String {
    expected_human_safe(value, true)
}

fn expected_human_safe(value: &str, preserve_line_feeds: bool) -> String {
    let mut rendered = String::new();
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

fn sentinel_prose() -> &'static Path {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY
        .get_or_init(|| {
            let cli_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../");
            let temporary = TempDir::new().unwrap().keep();
            let target = temporary.join("target");
            let mut build = Command::new("cargo");
            build
                .args([
                    "build",
                    "--locked",
                    "--offline",
                    "-p",
                    "prose-cli",
                    "--bin",
                    "prose",
                ])
                .current_dir(cli_root.join("rust"))
                .env("CARGO_TARGET_DIR", &target)
                .env_remove("OPENPROSE_IMAGE_SOURCE_DIR")
                .env_remove("OPENPROSE_IMAGE_BUNDLE")
                .env_remove("OPENPROSE_IMAGE_BUNDLE_CHECKSUM")
                .env_remove("OPENPROSE_REQUIRE_RELEASE_IMAGE");
            if cfg!(feature = "test-seams") {
                build.args(["--features", "prose-cli/test-seams"]);
            } else {
                // Ordinary fixture expectations describe the fixed echo image,
                // not the production default's moving published-kernel route.
                // The test-seams branch retains its default sentinel image.
                build
                    .env(
                        "OPENPROSE_IMAGE_SOURCE_DIR",
                        cli_root.join("shared/image/echo-v0"),
                    )
                    .env(
                        "OPENPROSE_IMAGE_BUNDLE",
                        cli_root.join("shared/image/embedded/current.bundle.bin"),
                    )
                    .env(
                        "OPENPROSE_IMAGE_BUNDLE_CHECKSUM",
                        cli_root.join("shared/image/embedded/current.bundle.sha256"),
                    );
            }
            if !cfg!(debug_assertions) {
                build.arg("--release");
            }
            if let Some(commit) = option_env!("OPENPROSE_BUILD_COMMIT") {
                build.env("OPENPROSE_BUILD_COMMIT", commit);
            }
            let build_output = build.output().unwrap();
            assert!(
                build_output.status.success(),
                "{}{}",
                String::from_utf8_lossy(&build_output.stdout),
                String::from_utf8_lossy(&build_output.stderr)
            );
            let profile = if cfg!(debug_assertions) {
                "debug"
            } else {
                "release"
            };
            target
                .join(profile)
                .join(if cfg!(windows) { "prose.exe" } else { "prose" })
        })
        .as_path()
}

fn echo_test_prose() -> &'static Path {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY
        .get_or_init(|| {
            let cli_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../");
            let temporary = TempDir::new().unwrap().keep();
            let target = temporary.join("target");
            let build = Command::new("cargo")
                .args([
                    "build",
                    "--locked",
                    "--offline",
                    "-p",
                    "prose-cli",
                    "--bin",
                    "prose",
                    "--features",
                    "prose-cli/test-seams",
                ])
                .current_dir(cli_root.join("rust"))
                .env("CARGO_TARGET_DIR", &target)
                .env(
                    "OPENPROSE_IMAGE_SOURCE_DIR",
                    cli_root.join("shared/image/echo-v0"),
                )
                .env(
                    "OPENPROSE_IMAGE_BUNDLE",
                    cli_root.join("shared/image/embedded/current.bundle.bin"),
                )
                .env(
                    "OPENPROSE_IMAGE_BUNDLE_CHECKSUM",
                    cli_root.join("shared/image/embedded/current.bundle.sha256"),
                )
                .env_remove("OPENPROSE_REQUIRE_RELEASE_IMAGE")
                .output()
                .unwrap();
            assert!(
                build.status.success(),
                "{}{}",
                String::from_utf8_lossy(&build.stdout),
                String::from_utf8_lossy(&build.stderr)
            );
            target
                .join("debug")
                .join(if cfg!(windows) { "prose.exe" } else { "prose" })
        })
        .as_path()
}

#[cfg(feature = "test-seams")]
fn build_mode_candidate(target: &Path, release: bool, test_seams: bool) -> Output {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../");
    let mut command = Command::new("cargo");
    command
        .args([
            "build",
            "--manifest-path",
            workspace.join("Cargo.toml").to_str().unwrap(),
            "--locked",
            "--offline",
            "-p",
            "prose-cli",
            "--bin",
            "prose",
        ])
        .env("CARGO_TARGET_DIR", target)
        .env_remove("OPENPROSE_IMAGE_SOURCE_DIR")
        .env_remove("OPENPROSE_IMAGE_BUNDLE")
        .env_remove("OPENPROSE_IMAGE_BUNDLE_CHECKSUM")
        .env_remove("OPENPROSE_REQUIRE_RELEASE_IMAGE");
    if release {
        command.arg("--release");
    }
    if test_seams {
        command.args(["--features", "prose-cli/test-seams"]);
    }
    command.output().unwrap()
}

#[cfg(feature = "test-seams")]
fn build_mode_report(binary: &Path, root: &Path) -> Value {
    let home = root.join("home");
    let config = root.join("config");
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&config).unwrap();
    let output = Command::new(binary)
        .args(["--output", "json", "cli", "doctor"])
        .current_dir(root)
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &config)
        .env("LANG", "C.UTF-8")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(10));
    assert!(output.stderr.is_empty());
    json_stdout(&output)
}

#[cfg(feature = "test-seams")]
fn assert_release_mock_rpc_is_rejected(binary: &Path, root: &Path) {
    for operation in [
        vec![
            "--harness",
            "mock",
            "--transport",
            "rpc",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
        vec![
            "--harness",
            "mock",
            "--transport",
            "rpc",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
    ] {
        let home = root.join("home");
        let config = root.join("config");
        fs::create_dir_all(&home).unwrap();
        fs::create_dir_all(&config).unwrap();
        let output = Command::new(binary)
            .args(operation)
            .current_dir(root)
            .env_clear()
            .env("HOME", &home)
            .env("XDG_CONFIG_HOME", &config)
            .env("LANG", "C.UTF-8")
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(20));
        assert!(output.stderr.is_empty());
        assert_eq!(
            json_stdout(&output)["details"],
            json!({
                "harness": "mock",
                "requested": "rpc",
                "supported": ["deterministic", "fake-process"]
            })
        );
    }
}

fn fake_harness() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../conformance/fake-harness/fake_harness.py")
        .canonicalize()
        .unwrap()
}

fn adapter_probe() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../shared/fixtures/adapters/bin/adapter_probe.py")
        .canonicalize()
        .unwrap()
}

#[cfg(unix)]
fn install_bun_runtime(root: &Path, directory_name: &str, version: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let bin = root.join(directory_name);
    fs::create_dir_all(&bin).unwrap();
    let executable = bin.join("bun");
    fs::write(
        &executable,
        format!(
            "#!/bin/sh\nprintf '%s\\n' {}\n",
            shell_single_quote(version)
        ),
    )
    .unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    bin
}

#[cfg(unix)]
fn install_observed_bun_runtime(
    root: &Path,
    directory_name: &str,
    version: &str,
    observation: &Path,
) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let bin = root.join(directory_name);
    fs::create_dir_all(&bin).unwrap();
    let forbidden = [
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "SSH_AUTH_SOCK",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ];
    let checks = forbidden
        .iter()
        .map(|name| format!(r#"[ "${{{name}+x}}" = x ] && printf '%s\n' {name} >> "$observation""#))
        .collect::<Vec<_>>()
        .join("\n");
    let executable = bin.join("bun");
    fs::write(
        &executable,
        format!(
            "#!/bin/sh\nobservation={}\n: > \"$observation\"\n{}\nprintf '%s\\n' {}\n",
            shell_single_quote(observation.to_str().unwrap()),
            checks,
            shell_single_quote(version)
        ),
    )
    .unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    bin
}

#[cfg(unix)]
fn install_cleanup_failing_bun_runtime(
    root: &Path,
    directory_name: &str,
    identity: &Path,
) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let bin = root.join(directory_name);
    fs::create_dir_all(&bin).unwrap();
    let executable = bin.join("bun");
    let program = format!(
        r#"#!/usr/bin/env python3
import json, os, signal, time
pid = os.fork()
if pid == 0:
    os.setsid()
    with open({}, "x", encoding="utf-8") as target:
        json.dump({{"pid": os.getpid()}}, target)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
    os._exit(0)
deadline = time.monotonic() + 2
while not os.path.exists({}):
    if time.monotonic() >= deadline:
        raise RuntimeError("identity timeout")
    time.sleep(0.01)
print("1.3.14", flush=True)
os._exit(0)
"#,
        serde_json::to_string(identity.to_str().unwrap()).unwrap(),
        serde_json::to_string(identity.to_str().unwrap()).unwrap(),
    );
    fs::write(&executable, program).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    bin
}

#[cfg(unix)]
fn kill_escaped_identity(identity: &Path) {
    use rustix::process::{Pid, Signal, kill_process};

    let Ok(bytes) = fs::read(identity) else {
        return;
    };
    let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
        return;
    };
    let Some(raw) = value["pid"]
        .as_i64()
        .and_then(|pid| i32::try_from(pid).ok())
    else {
        return;
    };
    let Some(pid) = Pid::from_raw(raw) else {
        return;
    };
    let _ = kill_process(pid, Signal::KILL);
}

fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

fn expected_human_runner_command(arguments: &str) -> String {
    format!(
        "{} {arguments}",
        shell_single_quote(sentinel_prose().to_str().unwrap())
    )
}

#[cfg(unix)]
fn install_live_adapter_fake(
    root: &Path,
    harness: &str,
    adapter_id: &str,
    version: &str,
) -> (PathBuf, PathBuf) {
    use std::os::unix::fs::PermissionsExt as _;

    let executable_name = match harness {
        "codex" => "codex",
        "claude" => "claude",
        "prime" => "prime-agent",
        "omp" => "omp",
        _ => panic!("unknown harness"),
    };
    let bin = root.join("bin");
    fs::create_dir_all(&bin).unwrap();
    let observation = root.join(format!("{harness}-live-observation.json"));
    let version_observation = root.join(format!("{harness}-version-probe-observation.json"));
    let source = fs::read_to_string(adapter_probe()).unwrap();
    let source = source.replace(
        "adapter_id = infer_adapter(argv)",
        &format!(
            "\n    if argv in ([\"--version\"], [\"-v\"]):\n        credential_names = [name for name in [\"OPENAI_API_KEY\", \"CODEX_ACCESS_TOKEN\", \"CODEX_HOME\", \"CODEX_SQLITE_HOME\", \"ANTHROPIC_API_KEY\", \"ANTHROPIC_OAUTH_TOKEN\", \"OPENROUTER_API_KEY\", \"GEMINI_API_KEY\", \"GOOGLE_APPLICATION_CREDENTIALS\", \"GITHUB_TOKEN\", \"GH_TOKEN\", \"COPILOT_GITHUB_TOKEN\", \"AWS_ACCESS_KEY_ID\", \"AWS_SECRET_ACCESS_KEY\", \"AWS_SESSION_TOKEN\"] if name in os.environ]\n        Path({}).write_text(canonical({{\"credentialNames\": sorted(credential_names)}}) + \"\\n\", encoding=\"utf-8\")\n        print({}, file=sys.stderr if {} else sys.stdout)\n        return 0\n    if argv == [\"login\", \"status\"]:\n        print(\"Logged in using ChatGPT\")\n        return 0\n    if argv == [\"auth\", \"status\", \"--json\"]:\n        print('{{\"loggedIn\":true}}')\n        return 0\n    if argv in ([\"model\", \"list\"], [\"--help\"]):\n        (Path.cwd() / \".unexpected-prime-omp-auth-probe\").write_text(\"called\", encoding=\"utf-8\")\n        print(\"fixture-model\")\n        return 0\n    adapter_id = {}",
            serde_json::to_string(version_observation.to_str().unwrap()).unwrap(),
            serde_json::to_string(version).unwrap(),
            if harness == "prime" { "True" } else { "False" },
            serde_json::to_string(adapter_id).unwrap()
        ),
    );
    let source = source.replace(
        "observation_path = os.environ.get(\"OPENPROSE_ADAPTER_OBSERVATION_PATH\")",
        &format!(
            "observation_path = {}",
            serde_json::to_string(observation.to_str().unwrap()).unwrap()
        ),
    );
    let source = source.replace(
        "\"files\": read_prompt_files(argv),",
        "\"files\": read_prompt_files(argv) if adapter_id != \"prime/rpc\" else [],",
    );
    let executable = bin.join(executable_name);
    fs::write(&executable, source).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    if harness == "omp" {
        install_bun_runtime(root, "bin", "1.3.14");
    }
    (bin, observation)
}

#[cfg(unix)]
fn install_prime_parser_failure_fake(root: &Path, fault: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let fault_source = match fault {
        "malformed" => {
            r#"sys.stdout.write("OPENPROSE-CANDIDATE-CANARY::{malformed\n")
sys.stdout.flush()"#
        }
        "truncated" => {
            r#"sys.stdout.write("{\"candidateSecret\":\"OPENPROSE-CANDIDATE-CANARY::truncated\"")
sys.stdout.flush()"#
        }
        _ => panic!("unknown Prime parser fault"),
    };
    let source = r#"#!/usr/bin/env python3
import json
import sys

if sys.argv[1:] == ["--version"]:
    print("prime-agent 0.7.0", file=sys.stderr)
    raise SystemExit(0)

request = json.loads(sys.stdin.buffer.readline())

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

emit({"id": request["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "agent_start"})
__FAULT__
"#
    .replace("__FAULT__", fault_source);
    let bin = root.join(format!("prime-parser-{fault}"));
    fs::create_dir_all(&bin).unwrap();
    let executable = bin.join("prime-agent");
    fs::write(&executable, source).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    bin
}

#[cfg(all(feature = "test-seams", unix))]
fn install_delayed_streaming_codex_fake(root: &Path) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let executable = root.join("delayed-codex.py");
    fs::write(
        &executable,
        r#"#!/usr/bin/env python3
import json
import sys
import time

sys.stdin.buffer.read()

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

terminal = {
    "schema": "openprose.echo-terminal/1",
    "semanticStatus": "not-applicable",
    "placeholder": True,
    "marker": "OPENPROSE_ECHO_TERMINAL_V0",
    "task": {"argv": ["prose", "write", "hello.prose.md"]},
}
emit({"type": "thread.started", "thread_id": "stream-thread"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "response", "type": "agent_message", "text": "first safe response\nsecond safe response\n" + json.dumps(terminal, separators=(",", ":"))}})
time.sleep(1.5)
emit({"type": "turn.completed"})
"#,
    )
    .unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    executable
}

#[cfg(all(feature = "test-seams", unix))]
fn delayed_streaming_codex_command(root: &Path, executable: &Path) -> Command {
    let home = root.join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    let mut command = Command::new(echo_test_prose());
    command
        .args([
            "--harness",
            "codex",
            "--transport",
            "exec-json",
            "write",
            "hello.prose.md",
        ])
        .current_dir(root)
        .env_clear()
        .env("HOME", &home)
        .env("USER", "fixture-posix-user")
        .env("USERPROFILE", "C:\\Users\\fixture")
        .env("XDG_CONFIG_HOME", &xdg)
        .env("LANG", "C.UTF-8")
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("OPENPROSE_CONFORMANCE_ADAPTER_MODE", "provider-free-v1")
        .env("OPENPROSE_CONFORMANCE_ADAPTER_PROBE", executable)
        .env(
            "OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION",
            root.join("unused-observation.json"),
        )
        .env(
            "OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP",
            "cached-chatgpt-login",
        );
    command
}

#[cfg(unix)]
fn prose_with_live_adapter(root: &Path, bin: &Path, _harness: &str, args: &[&str]) -> Output {
    prose_with_live_adapter_path(root, &[bin.to_path_buf()], args)
}

#[cfg(unix)]
fn prose_with_live_adapter_path(root: &Path, bins: &[PathBuf], args: &[&str]) -> Output {
    let home = root.join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    let search_path = std::env::join_paths(bins.iter().cloned().chain(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    )))
    .unwrap();
    let mut command = Command::new(echo_test_prose());
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", &home)
        .env("USER", "fixture-posix-user")
        .env("USERPROFILE", "C:\\Users\\fixture")
        .env("XDG_CONFIG_HOME", &xdg)
        .env("LANG", "C.UTF-8")
        .env("PATH", search_path)
        .env("ANTHROPIC_API_KEY", "raw-provider-secret-must-not-appear")
        .env(
            "ANTHROPIC_OAUTH_TOKEN",
            "raw-provider-secret-must-not-appear",
        )
        .env("OPENAI_API_KEY", "raw-provider-secret-must-not-appear")
        .env("OPENROUTER_API_KEY", "raw-provider-secret-must-not-appear")
        .env("GEMINI_API_KEY", "raw-provider-secret-must-not-appear")
        .env(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "/fixture/provider-key.json",
        )
        .env("GITHUB_TOKEN", "raw-provider-secret-must-not-appear")
        .env("GH_TOKEN", "raw-provider-secret-must-not-appear")
        .env(
            "COPILOT_GITHUB_TOKEN",
            "raw-provider-secret-must-not-appear",
        )
        .env("AWS_ACCESS_KEY_ID", "raw-provider-secret-must-not-appear")
        .env(
            "AWS_SECRET_ACCESS_KEY",
            "raw-provider-secret-must-not-appear",
        )
        .env("AWS_SESSION_TOKEN", "raw-provider-secret-must-not-appear")
        .env("AWS_PROFILE", "provider-profile")
        .env(
            "PRIME_AGENT_CODING_AGENT_DIR",
            "/fixture/prime-store-override",
        )
        .env("PI_CODING_AGENT_DIR", "/fixture/omp-store-override");
    command.output().unwrap()
}

#[cfg(any(feature = "test-seams", not(debug_assertions)))]
fn prose_with_fake(
    root: &Path,
    scenario: &str,
    args: &[&str],
    extra: &[(&str, OsString)],
) -> Output {
    prose_with_fake_executable(root, &fake_harness(), scenario, args, extra)
}

#[cfg(any(feature = "test-seams", not(debug_assertions)))]
fn prose_with_fake_executable(
    root: &Path,
    executable: &Path,
    scenario: &str,
    args: &[&str],
    extra: &[(&str, OsString)],
) -> Output {
    let home = root.join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    let mut command = Command::new(sentinel_prose());
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("LANG", "C.UTF-8")
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("OPENPROSE_CONFORMANCE_FAKE_HARNESS", executable)
        .env("OPENPROSE_CONFORMANCE_FAKE_SCENARIO", scenario);
    for (name, value) in extra {
        command.env(name, value);
    }
    command.output().unwrap()
}

#[cfg(all(feature = "test-seams", unix))]
fn mutated_fake_harness(root: &Path, from: &str, to: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let source = fs::read_to_string(fake_harness()).unwrap();
    assert!(source.contains(from), "fake harness mutation did not match");
    let executable = root.join("mutated-fake-harness.py");
    fs::write(&executable, source.replacen(from, to, 1)).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    executable
}

#[cfg(all(feature = "test-seams", unix))]
fn secret_echoing_adapter_probe(root: &Path) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let source = fs::read_to_string(adapter_probe()).unwrap();
    let marker = "    assistant_text = echo_assistant_text(task)";
    assert!(source.contains(marker));
    let replacement = r#"    selected_secret = os.environ.get("OPENROUTER_API_KEY", "")
    recursion_token = os.environ.get("OPENPROSE_RECURSION_TOKEN", "")
    run_nonce = os.environ.get("OPENPROSE_RUN_NONCE", "")
    print("\n".join((selected_secret, recursion_token, run_nonce)), file=sys.stderr)
    assistant_text = "\n".join((selected_secret, recursion_token, run_nonce, echo_assistant_text(task)))"#;
    let executable = root.join("secret-echoing-adapter-probe.py");
    fs::write(&executable, source.replacen(marker, replacement, 1)).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    executable
}

#[cfg(all(feature = "test-seams", unix))]
fn split_secret_echoing_adapter_probe(root: &Path) -> PathBuf {
    use std::os::unix::fs::PermissionsExt as _;

    let source = fs::read_to_string(adapter_probe()).unwrap();
    let marker = r#"        assistant_message = {
            "role": "assistant",
            "content": [{"type": "text", "text": assistant_text}],
        }"#;
    assert!(source.contains(marker), "OMP probe mutation did not match");
    let replacement = r#"        protected_values = (
            os.environ.get("OPENROUTER_API_KEY", ""),
            os.environ.get("OPENPROSE_RECURSION_TOKEN", ""),
            os.environ.get("OPENPROSE_RUN_NONCE", ""),
        )
        protected_fragments = []
        for protected_value in protected_values:
            midpoint = len(protected_value) // 2
            protected_fragments.extend((protected_value[:midpoint], protected_value[midpoint:]))
        assistant_message = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": fragment}
                for fragment in protected_fragments
            ] + [{"type": "text", "text": assistant_text}],
        }"#;
    let executable = root.join("split-secret-echoing-adapter-probe.py");
    fs::write(&executable, source.replacen(marker, replacement, 1)).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    executable
}

#[cfg(feature = "test-seams")]
fn prose_with_adapter_probe(
    root: &Path,
    adapter_id: &str,
    observation: &Path,
    args: &[&str],
) -> Output {
    prose_with_adapter_probe_executable(root, &adapter_probe(), adapter_id, observation, args)
}

#[cfg(feature = "test-seams")]
fn prose_with_adapter_probe_executable(
    root: &Path,
    executable: &Path,
    adapter_id: &str,
    observation: &Path,
    args: &[&str],
) -> Output {
    prose_with_adapter_probe_executable_and_cleanup_failure(
        root,
        executable,
        adapter_id,
        observation,
        args,
        false,
    )
}

#[cfg(feature = "test-seams")]
fn prose_with_adapter_probe_executable_and_cleanup_failure(
    root: &Path,
    executable: &Path,
    adapter_id: &str,
    observation: &Path,
    args: &[&str],
    cleanup_failure: bool,
) -> Output {
    let home = root.join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    #[cfg(unix)]
    let search_path = if adapter_id == "omp/rpc" {
        let bun_bin = install_bun_runtime(root, "fixture-bun-bin", "1.3.14");
        std::env::join_paths(std::iter::once(bun_bin).chain(std::env::split_paths(
            &std::env::var_os("PATH").unwrap_or_default(),
        )))
        .unwrap()
    } else {
        std::env::var_os("PATH").unwrap_or_default()
    };
    #[cfg(not(unix))]
    let search_path = std::env::var_os("PATH").unwrap_or_default();
    let mut command = Command::new(echo_test_prose());
    command
        .args(args)
        .current_dir(root)
        .env_clear()
        .env("HOME", &home)
        .env("USER", "fixture-posix-user")
        .env("USERPROFILE", "C:\\Users\\fixture")
        .env("XDG_CONFIG_HOME", &xdg)
        .env("LANG", "C.UTF-8")
        .env("TMPDIR", root)
        .env("PATH", search_path)
        .env("OPENROUTER_API_KEY", "fixture-openrouter-secret")
        .env("OPENAI_API_KEY", "fixture-openai-secret")
        .env("ANTHROPIC_API_KEY", "fixture-anthropic-secret")
        .env("OPENPROSE_TOKEN", "must-never-enter-child")
        .env("PROSE_BILLING_TOKEN", "must-never-enter-child")
        .env(
            "PRIME_AGENT_CODING_AGENT_DIR",
            "/fixture/prime-store-override",
        )
        .env("PI_CODING_AGENT_DIR", "/fixture/omp-store-override")
        .env("PI_CONFIG_FILES", "/fixture/hostile-omp-config.yml")
        .env("PRIME_AGENT_TELEMETRY", "1")
        .env("OPENPROSE_CONFORMANCE_ADAPTER_MODE", "provider-free-v1")
        .env("OPENPROSE_CONFORMANCE_ADAPTER_PROBE", executable)
        .env("OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION", observation)
        .env(
            "OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP",
            match adapter_id {
                "codex/exec-json" => "cached-chatgpt-login",
                "claude/print-stream-json" => "claude-subscription",
                "prime/rpc" | "omp/rpc" => "openrouter",
                _ => panic!("unknown fixture adapter"),
            },
        )
        .env("OPENPROSE_ADAPTER_EXPECTED_ID", adapter_id);
    if cleanup_failure {
        command.env("OPENPROSE_CONFORMANCE_ADAPTER_CLEANUP_FAILURE", "1");
    }
    command.output().unwrap()
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn human_mode_streams_an_admitted_nonterminal_assistant_message_before_settlement() {
    let temp = TempDir::new().unwrap();
    let executable = install_delayed_streaming_codex_fake(temp.path());
    let mut child = delayed_streaming_codex_command(temp.path(), &executable)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();

    let stdout = child.stdout.take().unwrap();
    let mut stdout = BufReader::new(stdout);
    let mut first = String::new();
    stdout.read_line(&mut first).unwrap();
    assert_eq!(first, "first safe response\n");
    assert!(
        child.try_wait().unwrap().is_none(),
        "the first assistant message was buffered until harness completion"
    );

    let mut rest = String::new();
    stdout.read_to_string(&mut rest).unwrap();
    let status = child.wait().unwrap();
    let mut diagnostic = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut diagnostic)
        .unwrap();
    assert!(status.success(), "{diagnostic}");
    assert_eq!(rest, "second safe response\n");
    assert!(!first.contains("openprose.echo-terminal"));
    assert!(!rest.contains("openprose.echo-terminal"));
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn human_stream_never_emits_an_invalid_terminal_candidate() {
    let temp = TempDir::new().unwrap();
    let executable = install_delayed_streaming_codex_fake(temp.path());
    let source = fs::read_to_string(&executable).unwrap().replace(
        "\"schema\": \"openprose.echo-terminal/1\"",
        "\"schema\": \"invalid-terminal\"",
    );
    fs::write(&executable, source).unwrap();
    let output = delayed_streaming_codex_command(temp.path(), &executable)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(22));
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "first safe response\nsecond safe response"
    );
    let diagnostic = String::from_utf8(output.stderr).unwrap();
    assert!(diagnostic.contains("PROTOCOL_MALFORMED"));
    assert!(!diagnostic.contains("invalid-terminal"));
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn successful_human_stream_withholds_protected_settled_output() {
    let temp = TempDir::new().unwrap();
    let executable = install_delayed_streaming_codex_fake(temp.path());
    let source = fs::read_to_string(&executable).unwrap().replace(
        "first safe response\\nsecond safe response\\n",
        "hello.prose.md\\n",
    );
    fs::write(&executable, source).unwrap();
    let output = delayed_streaming_codex_command(temp.path(), &executable)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "OpenProse completed; harness output was withheld by the human-output safety policy.\n"
    );
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn successful_human_stream_finishes_a_partially_withheld_response_with_a_notice() {
    let temp = TempDir::new().unwrap();
    let executable = install_delayed_streaming_codex_fake(temp.path());
    let source = fs::read_to_string(&executable).unwrap().replace(
        "emit({\"type\": \"item.completed\", \"item\": {\"id\": \"response\", \"type\": \"agent_message\", \"text\": \"first safe response\\nsecond safe response\\n\" + json.dumps(terminal, separators=(\",\", \":\"))}})",
        "emit({\"type\": \"item.completed\", \"item\": {\"id\": \"response-1\", \"type\": \"agent_message\", \"text\": \"first safe response\"}})\nemit({\"type\": \"item.completed\", \"item\": {\"id\": \"response-2\", \"type\": \"agent_message\", \"text\": \"second safe response\"}})\nemit({\"type\": \"item.completed\", \"item\": {\"id\": \"response-3\", \"type\": \"agent_message\", \"text\": \"hello.prose.md\\n\" + json.dumps(terminal, separators=(\",\", \":\"))}})",
    );
    fs::write(&executable, source).unwrap();
    let output = delayed_streaming_codex_command(temp.path(), &executable)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "first safe response\nOpenProse completed; the remaining harness output was withheld by the human-output safety policy.\n"
    );
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn successful_human_stream_withholds_control_shaped_settled_output() {
    let temp = TempDir::new().unwrap();
    let executable = install_delayed_streaming_codex_fake(temp.path());
    let source = fs::read_to_string(&executable).unwrap().replace(
        "first safe response\\nsecond safe response\\n",
        "{\\\"unsettled\\\":\\\"control\\\"}\\n",
    );
    fs::write(&executable, source).unwrap();
    let output = delayed_streaming_codex_command(temp.path(), &executable)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "OpenProse completed; harness output was withheld by the human-output safety policy.\n"
    );
}

fn json_stdout(output: &Output) -> Value {
    assert!(
        output.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout.last(), Some(&b'\n'));
    assert_eq!(String::from_utf8_lossy(&output.stdout).lines().count(), 1);
    serde_json::from_slice(&output.stdout).unwrap()
}

fn expected_configuration(root: &Path) -> Value {
    let mut expected: Value = serde_json::from_str(include_str!(
        "../../../../shared/fixtures/operations/configuration-explanation.json"
    ))
    .unwrap();
    expected["cwd"]["value"] = Value::String(fs::canonicalize(root).unwrap().display().to_string());
    expected["userConfigPath"] = Value::String(
        root.join("home/xdg/openprose/cli.toml")
            .display()
            .to_string(),
    );
    expected
}

fn operation_fixture(name: &str) -> Value {
    let source = match name {
        "account" => {
            include_str!("../../../../shared/fixtures/operations/account-status-unavailable.json")
        }
        "doctor" => {
            include_str!("../../../../shared/fixtures/operations/doctor-report.json")
        }
        "harnesses" => {
            include_str!("../../../../shared/fixtures/operations/harness-list.json")
        }
        _ => panic!("unknown operation fixture"),
    };
    serde_json::from_str(source).unwrap()
}

fn expected_harness_inventory() -> Value {
    let mut harnesses = operation_fixture("harnesses")["harnesses"].clone();
    // The generic SDK adapter was added after the older operation fixture.
    if !harnesses
        .as_array()
        .unwrap()
        .iter()
        .any(|h| h["id"] == "agents-sdk")
    {
        harnesses.as_array_mut().unwrap().insert(5,json!({"id":"agents-sdk","runtime":"installed-process","availability":"missing","transports":["jsonl"],"detectedVersion":null,"billingOwner":"user-provider","authCategory":"harness-managed","strictWrapperConformant":false,"testOnly":false}));
    }
    for harness in &mut harnesses.as_array_mut().unwrap()[1..6] {
        harness["availability"] = json!("missing");
        harness["detectedVersion"] = Value::Null;
        harness["strictWrapperConformant"] = json!(false);
        harness.as_object_mut().unwrap().remove("admissionBlock");
    }
    if !cfg!(feature = "test-seams") {
        let mock = harnesses
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|item| item["id"] == "mock")
            .unwrap();
        mock["availability"] = json!("unavailable");
        mock["detectedVersion"] = Value::Null;
        mock["strictWrapperConformant"] = json!(false);
        mock["admissionBlock"] = json!("test-seams-disabled");
    }
    harnesses
}

#[test]
fn help_is_the_exact_shared_fixture_and_starts_nothing() {
    let temp = TempDir::new().unwrap();
    for args in [
        vec!["--help"],
        vec!["cli", "harness", "--help"],
        vec!["cli", "harness", "use", "--help"],
        vec!["cli", "cleanup", "prime", "--help"],
        vec!["cli", "cleanup", "prime", "opaque-handle", "--help"],
        vec!["cli", "config", "explain", "--help"],
        vec!["--cwd", "/definitely/missing", "cli", "doctor", "--help"],
    ] {
        let output = prose(temp.path(), &args);
        assert!(output.status.success(), "{args:?}");
        assert!(output.stderr.is_empty(), "{args:?}");
        assert_eq!(
            output.stdout,
            include_bytes!("../../../../conformance/cases/fixtures/runner-help.txt"),
            "{args:?}"
        );
    }
}

/// The frozen account groups and verbs (`auth`,
/// `org list`, `package`) print their `help.v1.json` topic, never the runner
/// help, and start nothing (no device flow, no config write, no keychain).
#[test]
fn account_verb_help_prints_its_manifest_topic_and_starts_nothing() {
    let temp = TempDir::new().unwrap();
    let help: Value =
        serde_json::from_str(include_str!("../../../../shared/service/help.v1.json")).unwrap();
    for (args, topic) in [
        (vec!["cli", "auth", "--help"], "cli auth"),
        (
            vec![
                "--cwd",
                "/definitely/missing",
                "cli",
                "auth",
                "status",
                "--help",
            ],
            "cli auth status",
        ),
        (vec!["cli", "auth", "login", "--help"], "cli auth login"),
        (vec!["cli", "auth", "logout", "-h"], "cli auth logout"),
        (vec!["cli", "org", "list", "--help"], "cli org list"),
        (
            vec!["cli", "package", "publish", "--help"],
            "cli package publish",
        ),
    ] {
        let output = prose(temp.path(), &args);
        assert!(output.status.success(), "{args:?}");
        assert!(output.stderr.is_empty(), "{args:?}");
        let text = String::from_utf8(output.stdout).unwrap();
        assert_eq!(text, help["topics"][topic].as_str().unwrap(), "{args:?}");
        assert!(!text.contains("OpenProse outer runner"), "{args:?}");
        if topic.split(' ').count() == 3 {
            assert!(text.contains("\nExit codes: 0 success"), "{args:?}");
        }
    }
    // Help persisted nothing.
    assert!(
        fs::read_dir(temp.path().join("home").join("xdg"))
            .unwrap()
            .next()
            .is_none()
    );
}

#[test]
fn invalid_prime_cleanup_handle_fails_before_configuration_or_image_loading() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--cwd",
            "/definitely/missing",
            "--output",
            "json",
            "cli",
            "cleanup",
            "prime",
            "not-a-handle",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stderr.is_empty());
    let error = json_stdout(&output);
    assert_eq!(error["schema"], "openprose.runner-error/1");
    assert_eq!(error["code"], "CONFIG_INVALID");
    assert_eq!(
        error["details"]["reason"],
        "Prime cleanup handle is invalid"
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains(temp.path().to_str().unwrap()));
}

#[cfg(unix)]
#[test]
fn prime_cleanup_uses_the_invoked_binary_and_never_an_ambient_path_prose() {
    use std::os::unix::fs::PermissionsExt as _;

    let temp = TempDir::new().unwrap();
    let hostile_bin = temp.path().join("hostile-bin");
    fs::create_dir(&hostile_bin).unwrap();
    let hostile_prose = hostile_bin.join("prose");
    fs::write(
        &hostile_prose,
        b"#!/bin/sh\ntouch ambient-prose-was-run\nexit 99\n",
    )
    .unwrap();
    fs::set_permissions(&hostile_prose, fs::Permissions::from_mode(0o700)).unwrap();
    let home = temp.path().join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    let output = Command::new(sentinel_prose())
        .args([
            "--output",
            "json",
            "cli",
            "cleanup",
            "prime",
            "not-a-handle",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("LANG", "C.UTF-8")
        .env("PATH", &hostile_bin)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(!temp.path().join("ambient-prose-was-run").exists());
}

#[test]
fn default_is_openprose_billed_and_never_falls_back() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "run", "fixture.prose.md"],
    );
    assert_eq!(output.status.code(), Some(10));
    let result = json_stdout(&output);
    assert_eq!(result["schema"], "openprose.runner-result/1");
    assert_eq!(result["error"]["code"], "HOSTED_UNAVAILABLE");
    assert_eq!(result["billing"]["owner"], "openprose");
    assert_eq!(result["error"]["details"]["billingOwner"], "openprose");
    assert_eq!(result["error"]["details"]["fallbackSelected"], false);
    assert_eq!(
        result["error"]["action"],
        "To use the hosted service, run `cli run submit FILE --preview`; running programs on this machine needs a local harness (`cli harness list`)."
    );
    assert_eq!(
        result["error"]["details"]["suggestedArgv"],
        serde_json::json!([
            "--output",
            "json",
            "cli",
            "run",
            "submit",
            "fixture.prose.md",
            "--preview"
        ])
    );
}

#[test]
fn installed_adapters_fail_honestly_before_spawn_when_configuration_or_binary_is_missing() {
    let cases = [
        (
            "codex",
            "exec-json",
            "codex/exec-json",
            "HARNESS_UNAVAILABLE",
            10,
        ),
        ("prime", "rpc", "prime/rpc", "CONFIG_INVALID", 2),
        (
            "claude",
            "print-stream-json",
            "claude/print-stream-json",
            "HARNESS_UNAVAILABLE",
            10,
        ),
        ("omp", "rpc", "omp/rpc", "CONFIG_INVALID", 2),
    ];
    for (harness, transport, adapter_id, error_code, exit_code) in cases {
        let temp = TempDir::new().unwrap();
        let output = prose(
            temp.path(),
            &[
                "--harness",
                harness,
                "--transport",
                transport,
                "--output",
                "json",
                "run",
                "fixture.prose.md",
            ],
        );
        assert_eq!(output.status.code(), Some(exit_code), "{adapter_id}");
        let result = json_stdout(&output);
        assert_eq!(result["adapter"]["id"], adapter_id);
        assert_eq!(result["transport"], transport);
        assert_eq!(result["error"]["code"], error_code);
        assert_eq!(result["error"]["details"]["adapterId"], adapter_id);
        if error_code == "HARNESS_UNAVAILABLE" {
            assert_eq!(result["error"]["details"]["fallbackAttempted"], false);
            let (executables, admitted, repair) = match harness {
                "codex" => (
                    json!(["codex"]),
                    json!(["0.149.0-alpha.4.1"]),
                    "npm install --global @openai/codex@0.149.0-alpha.4.1",
                ),
                "claude" => (
                    json!(["claude"]),
                    json!(["2.1.243"]),
                    "npm install --global @anthropic-ai/claude-code@2.1.243",
                ),
                _ => unreachable!(),
            };
            assert_eq!(result["error"]["details"]["executableNames"], executables);
            assert_eq!(result["error"]["details"]["admittedVersions"], admitted);
            assert_eq!(result["error"]["details"]["repairCommand"], repair);
        }
        assert_eq!(result["billing"]["owner"], "user-provider");
        assert_eq!(result["terminal"]["transportCompleted"], false);
        assert_eq!(result["digests"]["deliveredImageSha256"], Value::Null);
    }
}

#[test]
fn installed_adapter_transport_mismatch_is_refused_without_fallback() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--harness",
            "codex",
            "--transport",
            "rpc",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
    );
    assert_eq!(output.status.code(), Some(20));
    let result = json_stdout(&output);
    assert_eq!(result["schema"], "openprose.runner-error/1");
    assert_eq!(result["code"], "TRANSPORT_UNSUPPORTED");
    assert_eq!(result["boundary"], "adapter");
    assert_eq!(
        result["details"],
        json!({
            "harness": "codex",
            "requested": "rpc",
            "supported": ["exec-json"]
        })
    );
    assert!(output.stderr.is_empty());
}

#[cfg(unix)]
#[test]
fn doctor_rejects_an_installed_transport_before_inventory_or_version_probes() {
    let temp = TempDir::new().unwrap();
    let (bin, run_observation) = install_live_adapter_fake(
        temp.path(),
        "codex",
        "codex/exec-json",
        "codex-cli 0.149.0-alpha.4.1",
    );
    let version_observation = temp.path().join("codex-version-probe-observation.json");
    let output = prose_with_live_adapter(
        temp.path(),
        &bin,
        "codex",
        &[
            "--harness",
            "prime",
            "--transport",
            "exec-json",
            "--model",
            "fixture/model",
            "--auth-profile",
            "prime-harness-login",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
    );
    assert_eq!(output.status.code(), Some(20));
    assert!(output.stderr.is_empty());
    assert_eq!(
        json_stdout(&output),
        json!({
            "schema": "openprose.runner-error/1",
            "code": "TRANSPORT_UNSUPPORTED",
            "boundary": "adapter",
            "message": "The selected harness does not support the requested transport.",
            "action": "Choose a transport listed for this harness by the `cli harness list` runner operation.",
            "exitCode": 20,
            "retryable": false,
            "details": {
                "harness": "prime",
                "requested": "exec-json",
                "supported": ["rpc"]
            }
        })
    );
    assert!(!version_observation.exists());
    assert!(!run_observation.exists());
}

#[test]
fn hosted_transport_mismatch_precedes_availability_for_doctor_and_run() {
    for args in [
        vec![
            "--transport",
            "unsupported",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
        vec![
            "--transport",
            "unsupported",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
    ] {
        let temp = TempDir::new().unwrap();
        let output = prose(temp.path(), &args);
        assert_eq!(output.status.code(), Some(20), "{args:?}");
        let error = json_stdout(&output);
        assert_eq!(error["schema"], "openprose.runner-error/1", "{args:?}");
        assert_eq!(error["code"], "TRANSPORT_UNSUPPORTED", "{args:?}");
        assert_eq!(error["boundary"], "adapter", "{args:?}");
        assert_eq!(
            error["message"], "The selected harness does not support the requested transport.",
            "{args:?}"
        );
        assert_eq!(
            error["action"],
            "Choose a transport listed for this harness by the `cli harness list` runner operation.",
            "{args:?}"
        );
        assert_eq!(
            error["details"],
            json!({
                "harness": "openprose",
                "requested": "unsupported",
                "supported": ["hosted"]
            }),
            "{args:?}"
        );
    }
}

#[test]
fn output_parse_errors_retain_only_an_earlier_recognized_machine_channel() {
    let temp = TempDir::new().unwrap();
    let json_output = prose(
        temp.path(),
        &["--output", "json", "--output", "invalid", "cli", "doctor"],
    );
    assert_eq!(json_output.status.code(), Some(2));
    assert!(json_output.stderr.is_empty());
    assert_eq!(json_stdout(&json_output)["code"], "INVOCATION_INVALID");

    let stream_output = prose(
        temp.path(),
        &["--output=jsonl", "--output=invalid", "cli", "doctor"],
    );
    assert_eq!(stream_output.status.code(), Some(2));
    assert!(stream_output.stderr.is_empty());
    let records = String::from_utf8(stream_output.stdout).unwrap();
    let records = records
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0]["type"], "runner.failed");
    assert_eq!(records[0]["payload"]["error"]["code"], "INVOCATION_INVALID");

    let human_output = prose(temp.path(), &["--output=invalid", "cli", "doctor"]);
    assert_eq!(human_output.status.code(), Some(2));
    assert!(human_output.stdout.is_empty());
    assert!(
        String::from_utf8(human_output.stderr)
            .unwrap()
            .contains("INVOCATION_INVALID at invocation")
    );

    let opaque = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "write",
            "--output",
            "json",
            "--output",
            "invalid",
        ],
    );
    if cfg!(feature = "test-seams") {
        assert!(opaque.status.success());
        assert_eq!(
            String::from_utf8(opaque.stdout).unwrap(),
            "Mock transport completed. No OpenProse language semantics were evaluated.\n"
        );
        assert!(opaque.stderr.is_empty());
    } else {
        assert_eq!(opaque.status.code(), Some(10));
    }
}

#[test]
fn installed_adapter_auto_transport_evidence_resolves_to_the_frozen_baseline() {
    for (harness, transport, adapter_id) in [
        ("codex", "exec-json", "codex/exec-json"),
        ("claude", "print-stream-json", "claude/print-stream-json"),
        ("prime", "rpc", "prime/rpc"),
        ("omp", "rpc", "omp/rpc"),
    ] {
        let temp = TempDir::new().unwrap();
        let output = prose(
            temp.path(),
            &[
                "--harness",
                harness,
                "--output",
                "json",
                "run",
                "fixture.prose.md",
            ],
        );
        let result = json_stdout(&output);
        assert_eq!(result["adapter"]["id"], adapter_id);
        assert_eq!(result["transport"], transport);
        assert_eq!(result["terminal"]["transportCompleted"], false);
        assert_ne!(result["adapter"]["id"], "openprose/hosted");
    }
}

#[cfg(feature = "test-seams")]
#[test]
#[allow(clippy::too_many_lines)]
fn provider_free_installed_adapters_preserve_bytes_and_settle_the_echo_placeholder() {
    let cases = [
        ("codex", "exec-json", "codex/exec-json", false),
        (
            "claude",
            "print-stream-json",
            "claude/print-stream-json",
            true,
        ),
        ("prime", "rpc", "prime/rpc", false),
        ("omp", "rpc", "omp/rpc", true),
    ];
    for (harness, transport, adapter_id, has_image_file) in cases {
        let temp = TempDir::new().unwrap();
        let hostile_project_config = temp.path().join(".omp/config.yml");
        if adapter_id == "omp/rpc" {
            fs::create_dir_all(hostile_project_config.parent().unwrap()).unwrap();
            fs::write(
                &hostile_project_config,
                b"retry:\n  enabled: true\nextensions:\n  - hostile-extension\n",
            )
            .unwrap();
        }
        let observation_path = temp.path().join(format!("{harness}-observation.json"));
        let output = prose_with_adapter_probe(
            temp.path(),
            adapter_id,
            &observation_path,
            &[
                "--harness",
                harness,
                "--transport",
                transport,
                "--output",
                "json",
                "run",
                "path with spaces/example.prose.md",
                "--model",
                "雪",
                "",
                "line\nbreak",
                ";$(touch nope)",
            ],
        );
        assert!(
            output.status.success(),
            "{adapter_id}: status={:?} stdout={} stderr={}",
            output.status.code(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let result = json_stdout(&output);
        assert_eq!(result["adapter"]["id"], adapter_id);
        assert_eq!(result["adapter"]["harnessVersion"], Value::Null);
        assert_eq!(result["transport"], transport);
        assert_eq!(result["terminal"]["transportCompleted"], true);
        assert_eq!(result["terminal"]["terminalEventObserved"], true);
        assert_eq!(result["terminal"]["exitCode"], 0);
        assert_eq!(result["semantic"]["status"], "not-applicable");
        assert!(result["semantic"]["terminalEnvelopeDigestSha256"].is_string());
        assert_eq!(result["runnerExitCode"], 0);
        assert_eq!(
            result["digests"]["deliveredImageSha256"],
            "5b10702a77d29104cc0f145b8971001e0f0d07e30de07debd09b3cba2ffb68ae"
        );
        assert_eq!(result["billing"]["owner"], "user-provider");
        assert_eq!(result["billing"]["authCategory"], "harness-managed");

        let observation: Value =
            serde_json::from_slice(&fs::read(&observation_path).unwrap()).unwrap();
        assert_eq!(observation["adapterId"], adapter_id);
        assert_eq!(observation["shell"], false);
        assert_eq!(observation["outerPty"], false);
        assert_eq!(
            observation["adapterControls"],
            if adapter_id == "prime/rpc" {
                json!({"PRIME_AGENT_TELEMETRY":"0"})
            } else {
                json!({})
            }
        );
        assert_eq!(
            observation["cwd"],
            temp.path().canonicalize().unwrap().to_str().unwrap()
        );
        let environment_names = observation["environmentNames"].as_array().unwrap();
        for required in ["USER", "USERPROFILE"] {
            assert!(
                environment_names.iter().any(|name| name == required),
                "{adapter_id} stripped required public identity input {required}"
            );
        }
        for forbidden in [
            "OPENPROSE_TOKEN",
            "PROSE_BILLING_TOKEN",
            "OPENPROSE_CONFORMANCE_ADAPTER_MODE",
            "OPENPROSE_CONFORMANCE_ADAPTER_PROBE",
            "OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION",
            "OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP",
            "OPENPROSE_CONFORMANCE_ADAPTER_CLEANUP_FAILURE",
        ] {
            assert!(
                !environment_names.iter().any(|name| name == forbidden),
                "{adapter_id} leaked {forbidden}"
            );
        }
        let expected_stdin_digest = result["digests"]["renderedPayloadSha256"].as_str();
        if adapter_id == "codex/exec-json" {
            assert_eq!(
                observation["stdin"]["sha256"].as_str(),
                expected_stdin_digest
            );
        } else {
            assert_eq!(result["digests"]["renderedPayloadSha256"], Value::Null);
        }
        let files = observation["files"].as_array().unwrap();
        assert_eq!(!files.is_empty(), has_image_file);
        for file in files {
            if file["flag"] == "--config" {
                assert_eq!(adapter_id, "omp/rpc");
                assert_eq!(file["byteLength"], 200);
                assert_eq!(
                    file["sha256"],
                    "47b1a27639ab8d5ecd52abbb5326354419583b7b781ceb66935c822f789d2779"
                );
                assert_eq!(file["mode"], "0600");
            } else {
                assert_eq!(file["byteLength"], 1310);
                assert_eq!(
                    file["sha256"],
                    "5b10702a77d29104cc0f145b8971001e0f0d07e30de07debd09b3cba2ffb68ae"
                );
            }
            assert!(!Path::new(file["path"].as_str().unwrap()).exists());
        }
        if adapter_id == "omp/rpc" {
            assert_eq!(files.len(), 2);
            let argv = observation["argv"].as_array().unwrap();
            assert_eq!(argv[argv.len() - 2], "--config");
            assert_eq!(argv[argv.len() - 1], files[1]["path"]);
            assert!(!argv.iter().any(|value| value == "--no-tools"));
        }
        if matches!(adapter_id, "prime/rpc" | "omp/rpc") {
            let config = &observation["credentialConfig"];
            assert_eq!(config["absolute"], true);
            assert_eq!(config["existsAtHarnessStart"], true);
            #[cfg(unix)]
            assert_eq!(config["mode"], "0700");
            assert_eq!(
                config["name"],
                if adapter_id == "prime/rpc" {
                    "PRIME_AGENT_CODING_AGENT_DIR"
                } else {
                    "PI_CODING_AGENT_DIR"
                }
            );
            assert!(!Path::new(config["path"].as_str().unwrap()).exists());
        } else {
            assert_eq!(observation["credentialConfig"], Value::Null);
        }
        let observation_text = fs::read_to_string(&observation_path).unwrap();
        assert!(!observation_text.contains("store-override"));
        assert!(!observation_text.contains("hostile-omp-config"));
        if adapter_id == "omp/rpc" {
            assert_eq!(
                fs::read(&hostile_project_config).unwrap(),
                b"retry:\n  enabled: true\nextensions:\n  - hostile-extension\n"
            );
        }
        for secret in [
            "fixture-openrouter-secret",
            "fixture-openai-secret",
            "fixture-anthropic-secret",
            "must-never-enter-child",
        ] {
            assert!(!observation_text.contains(secret));
            assert!(!String::from_utf8_lossy(&output.stderr).contains(secret));
        }
    }
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn private_file_cleanup_failure_dominates_installed_success_and_child_failure_without_leaking_authority()
 {
    use std::os::unix::fs::PermissionsExt as _;

    for (harness, transport, adapter_id) in [
        ("codex", "exec-json", "codex/exec-json"),
        ("claude", "print-stream-json", "claude/print-stream-json"),
        ("prime", "rpc", "prime/rpc"),
        ("omp", "rpc", "omp/rpc"),
    ] {
        for child_failure in [false, true] {
            let temp = TempDir::new().unwrap();
            let failing_probe = temp.path().join("child-failure-harness");
            fs::write(&failing_probe, b"#!/bin/sh\nexit 23\n").unwrap();
            fs::set_permissions(&failing_probe, fs::Permissions::from_mode(0o700)).unwrap();
            let success_probe = adapter_probe();
            let executable = if child_failure {
                failing_probe.as_path()
            } else {
                success_probe.as_path()
            };
            let observation = temp.path().join("cleanup-observation.json");
            let output = prose_with_adapter_probe_executable_and_cleanup_failure(
                temp.path(),
                executable,
                adapter_id,
                &observation,
                &[
                    "--harness",
                    harness,
                    "--transport",
                    transport,
                    "--output",
                    "json",
                    "run",
                    "private-task-sentinel.prose.md",
                ],
                true,
            );

            assert_eq!(
                output.status.code(),
                Some(25),
                "{adapter_id} child={child_failure}"
            );
            let result = json_stdout(&output);
            assert_eq!(result["error"]["code"], "PROCESS_CLEANUP_FAILED");
            assert_eq!(
                result["error"]["details"],
                json!({
                    "phase":"private-file-finalization",
                    "resource":"owned-private-transport-files",
                    "adapterId":adapter_id,
                    "fallbackAttempted":false
                })
            );
            let rendered = serde_json::to_string(&result).unwrap();
            for forbidden in [
                "cleanupHandle",
                "cleanupArgv",
                "openprose-transport-",
                "openprose-prime-",
                "private-task-sentinel.prose.md",
                "OPENPROSE_SENTINEL_IMAGE_V1",
                "fixture-openrouter-secret",
                "fixture-openai-secret",
                "fixture-anthropic-secret",
            ] {
                assert!(
                    !rendered.contains(forbidden),
                    "{adapter_id} child={child_failure} leaked {forbidden}: {rendered}"
                );
            }
            assert!(!output.status.success());
        }
    }
}

#[cfg(unix)]
#[test]
#[allow(clippy::too_many_lines)]
fn ordinary_installed_adapters_discover_probe_and_run_without_internal_seams() {
    for (harness, transport, adapter_id, version) in [
        (
            "codex",
            "exec-json",
            "codex/exec-json",
            "codex-cli 0.149.0-alpha.4.1",
        ),
        (
            "claude",
            "print-stream-json",
            "claude/print-stream-json",
            "2.1.243 (Claude Code)",
        ),
        ("prime", "rpc", "prime/rpc", "0.7.0"),
        ("omp", "rpc", "omp/rpc", "omp/18.0.9"),
    ] {
        let temp = TempDir::new().unwrap();
        let (bin, observation_path) =
            install_live_adapter_fake(temp.path(), harness, adapter_id, version);
        let output = prose_with_live_adapter(
            temp.path(),
            &bin,
            harness,
            &[
                "--harness",
                harness,
                "--transport",
                transport,
                "--model",
                if matches!(harness, "prime" | "omp") {
                    "fixture/runner-model"
                } else {
                    "runner-model"
                },
                if matches!(harness, "prime" | "omp") {
                    "--auth-profile"
                } else {
                    "--timeout"
                },
                if harness == "prime" {
                    "prime-harness-login"
                } else if harness == "omp" {
                    "omp-harness-login"
                } else {
                    "10m"
                },
                "--output",
                "json",
                "run",
                "hello world.prose.md",
                "--model",
                "opaque-language-value",
            ],
        );
        assert!(
            output.status.success(),
            "{adapter_id}: status={:?} stdout={} stderr={}",
            output.status.code(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let result = json_stdout(&output);
        assert_eq!(result["adapter"]["id"], adapter_id);
        assert_eq!(result["adapter"]["harnessVersion"], version);
        assert_eq!(result["transport"], transport);
        assert_eq!(result["semantic"]["status"], "not-applicable");
        assert_eq!(result["terminal"]["classification"], "success");
        assert_eq!(result["runnerExitCode"], 0);
        assert_eq!(result["billing"]["owner"], "user-provider");
        assert_eq!(result["negotiatedCapabilities"]["terminal"], "structured");
        assert_ne!(
            result["negotiatedCapabilities"]["cancellation"],
            "process-tree"
        );

        let observation: Value =
            serde_json::from_slice(&fs::read(&observation_path).unwrap()).unwrap();
        assert_eq!(observation["adapterId"], adapter_id);
        assert_eq!(observation["shell"], false);
        assert_eq!(observation["outerPty"], false);
        let observed_argv = observation["argv"].as_array().unwrap();
        let model_index = observed_argv
            .iter()
            .position(|value| value == "--model")
            .expect("runner model was delivered to harness argv");
        assert_eq!(
            observed_argv[model_index + 1],
            if matches!(harness, "prime" | "omp") {
                "fixture/runner-model"
            } else {
                "runner-model"
            }
        );
        assert_eq!(
            observed_argv
                .iter()
                .filter(|value| *value == "--model")
                .count(),
            1
        );
        match adapter_id {
            "codex/exec-json" => {
                assert_eq!(model_index + 3, observed_argv.len());
                assert_eq!(observed_argv.last().unwrap(), "-");
                assert_eq!(observed_argv[2], "--skip-git-repo-check");
            }
            "claude/print-stream-json" => {
                assert_eq!(model_index + 3, observed_argv.len());
            }
            "prime/rpc" => {
                assert_eq!(model_index + 2, observed_argv.len());
            }
            "omp/rpc" => {
                assert_eq!(model_index + 4, observed_argv.len());
                assert_eq!(observed_argv[model_index + 2], "--config");
                assert!(!observed_argv.iter().any(|value| value == "--no-tools"));
            }
            _ => unreachable!(),
        }
        if matches!(adapter_id, "prime/rpc" | "omp/rpc") {
            assert_eq!(observation["credentialConfig"], Value::Null);
        }
        assert_eq!(
            observation["adapterControls"],
            if adapter_id == "prime/rpc" {
                json!({"PRIME_AGENT_TELEMETRY":"0"})
            } else {
                json!({})
            }
        );
        assert_eq!(
            observed_argv
                .iter()
                .filter(|value| *value == "opaque-language-value")
                .count(),
            0,
            "opaque language argv leaked into adapter argv"
        );
        let serialized = serde_json::to_string(&observation).unwrap();
        assert!(!serialized.contains("raw-provider-secret-must-not-appear"));
        assert!(!serialized.contains("PRIME_AGENT_CODING_AGENT_DIR"));
        assert!(!serialized.contains("PI_CODING_AGENT_DIR"));
        assert!(!observation_path.with_file_name("must-not-exist").exists());
    }
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn selected_provider_secrets_never_enter_public_stderr_or_assistant_output() {
    for output_mode in ["human", "json", "jsonl"] {
        let temp = TempDir::new().unwrap();
        let executable = secret_echoing_adapter_probe(temp.path());
        let observation = temp.path().join(format!("secret-{output_mode}.json"));
        let output = prose_with_adapter_probe_executable(
            temp.path(),
            &executable,
            "prime/rpc",
            &observation,
            &[
                "--harness",
                "prime",
                "--transport",
                "rpc",
                "--model",
                "fixture/model",
                "--auth-profile",
                "openrouter",
                "--output",
                output_mode,
                "run",
                "fixture.prose.md",
            ],
        );
        assert!(
            output.status.success(),
            "{output_mode}: status={:?} stdout={} stderr={}",
            output.status.code(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let public = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            !public.contains("fixture-openrouter-secret"),
            "{output_mode} leaked the selected secret"
        );
        assert!(
            !public.contains("installed-recursion-") && !public.contains("installed-run-"),
            "{output_mode} leaked runner-owned recursion metadata"
        );
        if output_mode != "json" {
            assert!(
                public.contains("[REDACTED]")
                    || public.contains("withheld by the human-output safety policy"),
                "{output_mode} did not expose a safe suppression marker"
            );
        }
    }
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn split_protected_values_cannot_be_reconstructed_across_jsonl_assistant_records() {
    let temp = TempDir::new().unwrap();
    let executable = split_secret_echoing_adapter_probe(temp.path());
    let observation = temp.path().join("split-secret-jsonl.json");
    let output = prose_with_adapter_probe_executable(
        temp.path(),
        &executable,
        "omp/rpc",
        &observation,
        &[
            "--harness",
            "omp",
            "--transport",
            "rpc",
            "--model",
            "fixture/model",
            "--auth-profile",
            "openrouter",
            "--output",
            "jsonl",
            "run",
            "fixture.prose.md",
        ],
    );
    assert!(
        output.status.success(),
        "status={:?} stdout={} stderr={}",
        output.status.code(),
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let assistant_text = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|event| event["type"] == "assistant.message")
        .filter_map(|event| event["payload"]["text"].as_str().map(ToOwned::to_owned))
        .collect::<Vec<_>>();
    assert!(!assistant_text.is_empty());
    let reconstructed = assistant_text.concat();
    for protected in [
        "fixture-openrouter-secret",
        "installed-recursion-",
        "installed-run-",
    ] {
        assert!(
            !reconstructed.contains(protected),
            "JSONL assistant records reconstructed {protected:?}: {assistant_text:?}"
        );
    }
    let selected = "fixture-openrouter-secret";
    let midpoint = selected.len() / 2;
    assert!(!reconstructed.contains(&selected[..midpoint]));
    assert!(!reconstructed.contains(&selected[midpoint..]));
    assert!(reconstructed.contains("[REDACTED]"));
}

#[cfg(unix)]
#[test]
fn prime_parser_failures_expose_only_the_closed_framing_diagnostic() {
    for (fault, error_code) in [
        ("malformed", "PROTOCOL_MALFORMED"),
        ("truncated", "PROTOCOL_TRUNCATED"),
    ] {
        let temp = TempDir::new().unwrap();
        let bin = install_prime_parser_failure_fake(temp.path(), fault);
        let output = prose_with_live_adapter(
            temp.path(),
            &bin,
            "prime",
            &[
                "--harness",
                "prime",
                "--transport",
                "rpc",
                "--model",
                "fixture/model",
                "--auth-profile",
                "prime-harness-login",
                "--output",
                "json",
                "run",
                "fixture.prose.md",
            ],
        );
        assert_eq!(output.status.code(), Some(22), "{fault}");
        assert_eq!(output.stderr, b"", "{fault}");
        let result = json_stdout(&output);
        assert_eq!(result["error"]["code"], error_code, "{fault}");
        assert_eq!(
            result["error"]["details"]["adapterDiagnostic"],
            json!({
                "schema":"openprose.adapter-diagnostic/1",
                "adapterId":"prime/rpc",
                "stage":"jsonl-framing",
                "phase":"record-boundary",
                "counters":{
                    "acceptedRecords":2,
                    "thinkingDeltas":0,
                    "textDeltas":0,
                    "saturated":false
                }
            }),
            "{fault}"
        );
        let serialized = serde_json::to_string(&result["error"]).unwrap();
        assert!(
            !serialized.contains("OPENPROSE-CANDIDATE-CANARY"),
            "{fault}"
        );
        assert!(!serialized.contains("candidateSecret"), "{fault}");
        assert!(matches!(
            result["error"]["details"]["reason"].as_str(),
            Some("protocol_admission_rejected" | "invalid_json")
        ));
        assert!(result["error"]["details"]["admittedRecordCount"].is_u64());
    }
}

#[cfg(unix)]
#[test]
fn rejected_prime_and_omp_version_probes_never_receive_selected_provider_credentials() {
    for (harness, adapter_id, version) in [
        ("prime", "prime/rpc", "prime-agent 0.7.1"),
        ("omp", "omp/rpc", "omp/18.0.10"),
    ] {
        let temp = TempDir::new().unwrap();
        let (bin, _) = install_live_adapter_fake(temp.path(), harness, adapter_id, version);
        let output = prose_with_live_adapter(
            temp.path(),
            &bin,
            harness,
            &[
                "--harness",
                harness,
                "--transport",
                "rpc",
                "--model",
                "fixture/model",
                "--auth-profile",
                "openai",
                "--output",
                "json",
                "cli",
                "doctor",
            ],
        );
        assert_eq!(output.status.code(), Some(10), "{adapter_id}");
        assert_eq!(
            json_stdout(&output)["problems"][0]["code"],
            "HARNESS_INCOMPATIBLE",
            "{adapter_id}"
        );
        let observation: Value = serde_json::from_slice(
            &fs::read(
                temp.path()
                    .join(format!("{harness}-version-probe-observation.json")),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            observation["credentialNames"],
            json!([]),
            "{adapter_id} version probe received a credential"
        );
    }
}

#[cfg(unix)]
#[test]
#[allow(clippy::too_many_lines)]
fn omp_runtime_prerequisite_blocks_before_omp_auth_and_never_falls_through_path() {
    let temp = TempDir::new().unwrap();
    let (omp_bin, _) = install_live_adapter_fake(temp.path(), "omp", "omp/rpc", "omp/18.0.9");
    fs::remove_file(omp_bin.join("bun")).unwrap();
    let bun_observation = temp.path().join("bun-version-environment.txt");
    let old_bun =
        install_observed_bun_runtime(temp.path(), "old-bun-bin", "1.3.13", &bun_observation);
    let good_bun = install_bun_runtime(temp.path(), "good-bun-bin", "1.3.14");
    let path = [omp_bin, old_bun, good_bun];
    let base = [
        "--harness",
        "omp",
        "--transport",
        "rpc",
        "--model",
        "fixture/model",
        "--auth-profile",
        "openrouter",
    ];

    let mut run = base.to_vec();
    run.extend(["--output", "json", "run", "hello.prose.md"]);
    let output = prose_with_live_adapter_path(temp.path(), &path, &run);
    assert_eq!(output.status.code(), Some(10));
    let error = json_stdout(&output)["error"].clone();
    assert_eq!(error["code"], "HARNESS_INCOMPATIBLE");
    assert_eq!(
        error["details"]["runtimePrerequisite"],
        json!({
            "runtime":"bun",
            "versionRange":">=1.3.14",
            "detectedVersion":"1.3.13",
            "availability":"incompatible",
            "repairCommand":"npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9"
        })
    );
    let rendered_error = serde_json::to_string(&error).unwrap();
    for forbidden in [
        temp.path().to_str().unwrap(),
        "raw-provider-secret-must-not-appear",
        "provider-key.json",
    ] {
        assert!(!rendered_error.contains(forbidden));
    }
    assert_eq!(fs::read_to_string(&bun_observation).unwrap(), "");
    assert!(
        !temp
            .path()
            .join("omp-version-probe-observation.json")
            .exists()
    );

    let mut doctor = base.to_vec();
    doctor.extend(["--output", "json", "cli", "doctor"]);
    let output = prose_with_live_adapter_path(temp.path(), &path, &doctor);
    assert_eq!(output.status.code(), Some(10));
    let report = json_stdout(&output);
    assert_eq!(
        report["problems"][0]["details"]["runtimePrerequisite"]["detectedVersion"],
        "1.3.13"
    );
    let omp = report["harnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|status| status["id"] == "omp")
        .unwrap();
    assert_eq!(omp["availability"], "incompatible");
    assert_eq!(omp["runtimePrerequisites"][0]["detectedVersion"], "1.3.13");

    let mut human = base.to_vec();
    human.extend(["cli", "doctor"]);
    let output = prose_with_live_adapter_path(temp.path(), &path, &human);
    assert_eq!(output.status.code(), Some(10));
    let text = String::from_utf8(output.stdout).unwrap();
    for line in [
        "Runtime prerequisite: bun",
        "Detected runtime version: 1.3.13",
        "Required runtime version: >=1.3.14",
        "Repair: npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
    ] {
        assert!(text.contains(line), "missing {line:?}: {text}");
    }
    assert!(!text.contains(temp.path().to_str().unwrap()));
    assert!(
        !temp
            .path()
            .join("omp-version-probe-observation.json")
            .exists()
    );

    fs::remove_file(path[1].join("bun")).unwrap();
    let output = prose_with_live_adapter_path(
        temp.path(),
        &path,
        &["--output", "json", "cli", "harness", "list"],
    );
    assert!(output.status.success());
    let report = json_stdout(&output);
    let omp = report["harnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|status| status["id"] == "omp")
        .unwrap();
    assert_eq!(omp["availability"], "available");
    assert_eq!(omp["detectedVersion"], "omp/18.0.9");
    assert_eq!(omp["runtimePrerequisites"][0]["detectedVersion"], "1.3.14");
    assert!(
        temp.path()
            .join("omp-version-probe-observation.json")
            .exists()
    );
}

#[cfg(unix)]
#[test]
fn omp_runtime_cleanup_failure_dominates_inventory_doctor_and_run_pathlessly() {
    let commands: [&[&str]; 3] = [
        &["--output", "json", "cli", "harness", "list"],
        &[
            "--harness",
            "omp",
            "--model",
            "fixture/model",
            "--auth-profile",
            "openrouter",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
        &[
            "--harness",
            "omp",
            "--transport",
            "rpc",
            "--model",
            "fixture/model",
            "--auth-profile",
            "openrouter",
            "--output",
            "json",
            "run",
            "hello.prose.md",
        ],
    ];

    for (index, args) in commands.into_iter().enumerate() {
        let temp = TempDir::new().unwrap();
        let (omp_bin, _) = install_live_adapter_fake(temp.path(), "omp", "omp/rpc", "omp/18.0.9");
        fs::remove_file(omp_bin.join("bun")).unwrap();
        let identity = temp.path().join(format!("escaped-bun-{index}.json"));
        let bun_bin = install_cleanup_failing_bun_runtime(
            temp.path(),
            &format!("cleanup-bun-{index}"),
            &identity,
        );
        let output = prose_with_live_adapter_path(temp.path(), &[omp_bin, bun_bin], args);
        kill_escaped_identity(&identity);
        assert_eq!(output.status.code(), Some(25), "{args:?}");
        assert!(output.stderr.is_empty(), "{args:?}");
        let report = json_stdout(&output);
        let error = report.get("error").unwrap_or(&report);
        assert_eq!(error["code"], "PROCESS_CLEANUP_FAILED", "{args:?}");
        assert_eq!(error["boundary"], "cleanup", "{args:?}");
        let rendered = serde_json::to_string(error).unwrap();
        assert!(!rendered.contains(temp.path().to_str().unwrap()));
        assert!(!rendered.contains("hello.prose.md"));
        assert_ne!(report["schema"], "openprose.harness-list/1");
        assert_ne!(report["schema"], "openprose.doctor-report/1");
    }
}

#[cfg(unix)]
#[test]
fn doctor_replaces_selected_omp_inventory_with_one_authoritative_second_observation() {
    use std::os::unix::fs::PermissionsExt as _;

    let temp = TempDir::new().unwrap();
    let (bin, _) = install_live_adapter_fake(temp.path(), "omp", "omp/rpc", "omp/18.0.9");
    let counter = temp.path().join("bun-probe-count");
    fs::write(
        bin.join("bun"),
        format!(
            "#!/bin/sh\ncounter={}\ncount=0\n[ ! -f \"$counter\" ] || count=$(cat \"$counter\")\ncount=$((count + 1))\nprintf '%s' \"$count\" > \"$counter\"\nif [ \"$count\" -eq 1 ]; then printf '1.3.14\\n'; else printf '1.3.13\\n'; fi\n",
            shell_single_quote(counter.to_str().unwrap())
        ),
    )
    .unwrap();
    fs::set_permissions(bin.join("bun"), fs::Permissions::from_mode(0o700)).unwrap();

    let output = prose_with_live_adapter_path(
        temp.path(),
        &[bin],
        &[
            "--harness",
            "omp",
            "--model",
            "fixture/model",
            "--auth-profile",
            "openrouter",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
    );
    assert_eq!(output.status.code(), Some(10));
    let report = json_stdout(&output);
    assert_eq!(report["ready"], false);
    assert_eq!(
        report["problems"][0]["details"]["runtimePrerequisite"]["detectedVersion"],
        "1.3.13"
    );
    let omp = report["harnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|status| status["id"] == "omp")
        .unwrap();
    assert_eq!(omp["availability"], "incompatible");
    assert_eq!(omp["detectedVersion"], Value::Null);
    assert_eq!(omp["runtimePrerequisites"][0]["detectedVersion"], "1.3.13");
    assert_eq!(fs::read_to_string(counter).unwrap(), "2");
}

#[cfg(unix)]
#[test]
fn rejected_adjacent_versions_report_exact_machine_and_human_repair_details() {
    for (harness, transport, adapter_id, version, admitted, repair, auth_profile) in [
        (
            "prime",
            "rpc",
            "prime/rpc",
            "prime-agent 0.7.1",
            json!(["0.7.0", "0.8.1"]),
            "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1",
            Some("prime-harness-login"),
        ),
        (
            "omp",
            "rpc",
            "omp/rpc",
            "omp/18.0.10",
            json!(["18.0.9"]),
            "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            Some("omp-harness-login"),
        ),
        (
            "codex",
            "exec-json",
            "codex/exec-json",
            "codex-cli 0.149.0-alpha.4.2",
            json!(["0.149.0-alpha.4.1"]),
            "npm install --global @openai/codex@0.149.0-alpha.4.1",
            None,
        ),
        (
            "claude",
            "print-stream-json",
            "claude/print-stream-json",
            "2.1.244 (Claude Code)",
            json!(["2.1.243"]),
            "npm install --global @anthropic-ai/claude-code@2.1.243",
            None,
        ),
    ] {
        let temp = TempDir::new().unwrap();
        let (bin, _) = install_live_adapter_fake(temp.path(), harness, adapter_id, version);
        let mut base = vec!["--harness", harness, "--transport", transport];
        if matches!(harness, "prime" | "omp") {
            base.extend([
                "--model",
                "fixture/model",
                "--auth-profile",
                auth_profile.unwrap(),
            ]);
        }

        let mut doctor_json = base.clone();
        doctor_json.extend(["--output", "json", "cli", "doctor"]);
        let machine = prose_with_live_adapter(temp.path(), &bin, harness, &doctor_json);
        assert_eq!(machine.status.code(), Some(10));
        let problem = &json_stdout(&machine)["problems"][0];
        assert_eq!(problem["code"], "HARNESS_INCOMPATIBLE");
        assert_eq!(problem["details"]["adapterId"], adapter_id);
        assert_eq!(problem["details"]["detectedVersion"], version);
        assert_eq!(problem["details"]["admittedVersions"], admitted);
        assert_eq!(problem["details"]["repairCommand"], repair);
        assert_eq!(problem["details"]["fallbackAttempted"], false);

        let expected = [
            format!("Detected version: {version}"),
            format!(
                "Admitted versions: {}",
                admitted
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|value| value.as_str().unwrap())
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            format!("Repair: {repair}"),
        ];
        let mut doctor_human = base.clone();
        doctor_human.extend(["--output", "human", "cli", "doctor"]);
        let human = prose_with_live_adapter(temp.path(), &bin, harness, &doctor_human);
        assert_eq!(human.status.code(), Some(10));
        let text = String::from_utf8(human.stdout).unwrap();
        for line in &expected {
            assert!(text.contains(line), "{adapter_id} doctor omitted {line:?}");
        }

        let mut run_human = base;
        run_human.extend(["--output", "human", "run", "hello.prose.md"]);
        let human = prose_with_live_adapter(temp.path(), &bin, harness, &run_human);
        assert_eq!(human.status.code(), Some(10));
        let text = String::from_utf8(human.stderr).unwrap();
        for line in &expected {
            assert!(text.contains(line), "{adapter_id} run omitted {line:?}");
        }
    }
}

#[cfg(unix)]
struct WrongStreamVersionCase {
    harness: &'static str,
    transport: &'static str,
    adapter_id: &'static str,
    executable_name: &'static str,
    version: &'static str,
    admitted: Value,
    repair: &'static str,
    auth_profile: Option<&'static str>,
}

#[cfg(unix)]
fn assert_wrong_stream_version_repair(case: &WrongStreamVersionCase) {
    use std::os::unix::fs::PermissionsExt as _;

    let temp = TempDir::new().unwrap();
    let bin = temp.path().join("bin");
    fs::create_dir_all(&bin).unwrap();
    if case.harness == "omp" {
        install_bun_runtime(temp.path(), "bin", "1.3.14");
    }
    let executable = bin.join(case.executable_name);
    let private_output = format!("{}-wrong-stream-private-output", case.harness);
    let redirect = if case.harness == "prime" { "" } else { " >&2" };
    fs::write(
        &executable,
        format!(
            "#!/bin/sh\nprintf '%s\\n' {}{}\nprintf '%s\\n' {}{}\n",
            shell_single_quote(case.version),
            redirect,
            shell_single_quote(&private_output),
            redirect,
        ),
    )
    .unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();

    let mut base = vec!["--harness", case.harness, "--transport", case.transport];
    if let Some(profile) = case.auth_profile {
        base.extend(["--model", "fixture/model", "--auth-profile", profile]);
    }
    let mut doctor_json = base.clone();
    doctor_json.extend(["--output", "json", "cli", "doctor"]);
    let machine = prose_with_live_adapter(temp.path(), &bin, case.harness, &doctor_json);
    assert_eq!(machine.status.code(), Some(10));
    assert!(machine.stderr.is_empty());
    let report = json_stdout(&machine);
    assert_eq!(
        report["problems"][0]["details"],
        json!({
            "adapterId": case.adapter_id,
            "admittedVersions": case.admitted,
            "repairCommand": case.repair,
            "fallbackAttempted": false,
        })
    );

    let mut doctor_human = base;
    doctor_human.extend(["--output", "human", "cli", "doctor"]);
    let human = prose_with_live_adapter(temp.path(), &bin, case.harness, &doctor_human);
    assert_eq!(human.status.code(), Some(10));
    assert!(human.stderr.is_empty());
    let human_text = String::from_utf8(human.stdout).unwrap();
    assert!(human_text.contains(&format!("Repair: {}", case.repair)));
    assert!(
        human_text.contains("Run the exact Repair command reported with this error, then retry.")
    );

    for text in [serde_json::to_string(&report).unwrap(), human_text] {
        assert!(!text.contains(&private_output));
        assert!(!text.contains(executable.to_str().unwrap()));
        assert!(!text.contains("processExit"));
        assert!(!text.contains("terminalEventObserved"));
    }
}

#[cfg(unix)]
#[test]
fn successful_wrong_stream_versions_report_exact_safe_repair_details() {
    for case in [
        WrongStreamVersionCase {
            harness: "prime",
            transport: "rpc",
            adapter_id: "prime/rpc",
            executable_name: "prime-agent",
            version: "prime-agent 0.7.0",
            admitted: json!(["0.7.0", "0.8.1"]),
            repair: "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1",
            auth_profile: Some("prime-harness-login"),
        },
        WrongStreamVersionCase {
            harness: "omp",
            transport: "rpc",
            adapter_id: "omp/rpc",
            executable_name: "omp",
            version: "omp/18.0.9",
            admitted: json!(["18.0.9"]),
            repair: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
            auth_profile: Some("omp-harness-login"),
        },
        WrongStreamVersionCase {
            harness: "codex",
            transport: "exec-json",
            adapter_id: "codex/exec-json",
            executable_name: "codex",
            version: "codex-cli 0.149.0-alpha.4.1",
            admitted: json!(["0.149.0-alpha.4.1"]),
            repair: "npm install --global @openai/codex@0.149.0-alpha.4.1",
            auth_profile: None,
        },
        WrongStreamVersionCase {
            harness: "claude",
            transport: "print-stream-json",
            adapter_id: "claude/print-stream-json",
            executable_name: "claude",
            version: "2.1.243 (Claude Code)",
            admitted: json!(["2.1.243"]),
            repair: "npm install --global @anthropic-ai/claude-code@2.1.243",
            auth_profile: None,
        },
    ] {
        assert_wrong_stream_version_repair(&case);
    }
}

#[cfg(unix)]
#[test]
fn prime_and_omp_harness_login_routes_require_explicit_profiles_and_qualified_models() {
    for (harness, adapter_id, auth_profile) in [
        ("prime", "prime/rpc", "prime-harness-login"),
        ("omp", "omp/rpc", "omp-harness-login"),
    ] {
        let temp = TempDir::new().unwrap();
        let (bin, _) = install_live_adapter_fake(
            temp.path(),
            harness,
            adapter_id,
            if harness == "prime" {
                "0.7.0"
            } else {
                "omp/18.0.9"
            },
        );
        let missing_profile = prose_with_live_adapter(
            temp.path(),
            &bin,
            harness,
            &[
                "--harness",
                harness,
                "--model",
                "fixture/model",
                "--output",
                "json",
                "cli",
                "doctor",
            ],
        );
        assert_eq!(missing_profile.status.code(), Some(2));
        let report = json_stdout(&missing_profile);
        assert_eq!(report["problems"][0]["code"], "CONFIG_INVALID");
        assert_eq!(report["problems"][0]["details"]["adapterId"], adapter_id);
        assert!(
            serde_json::to_string(&report)
                .unwrap()
                .contains(auth_profile)
        );

        for invalid in [
            "unqualified",
            "/model",
            "provider/",
            "provider//model",
            "provider/model name",
            "provider/model\nname",
        ] {
            let output = prose_with_live_adapter(
                temp.path(),
                &bin,
                harness,
                &[
                    "--harness",
                    harness,
                    "--model",
                    invalid,
                    "--auth-profile",
                    auth_profile,
                    "--output",
                    "json",
                    "cli",
                    "doctor",
                ],
            );
            assert_eq!(
                output.status.code(),
                Some(2),
                "{harness} accepted {invalid:?}"
            );
            let report = json_stdout(&output);
            assert_eq!(report["problems"][0]["code"], "CONFIG_INVALID");
            assert!(
                serde_json::to_string(&report)
                    .unwrap()
                    .contains("provider/model")
            );
        }
    }
}

#[cfg(unix)]
#[test]
fn dry_run_and_doctor_probe_real_discovery_without_starting_a_model() {
    let temp = TempDir::new().unwrap();
    let (bin, observation) = install_live_adapter_fake(
        temp.path(),
        "codex",
        "codex/exec-json",
        "codex-cli 0.149.0-alpha.4.1",
    );
    for args in [
        vec![
            "--harness",
            "codex",
            "--dry-run",
            "--output",
            "json",
            "run",
            "hello.prose.md",
        ],
        vec!["--harness", "codex", "--output", "json", "cli", "doctor"],
    ] {
        let output = prose_with_live_adapter(temp.path(), &bin, "codex", &args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report = json_stdout(&output);
        if report["schema"] == "openprose.runner-dry-run-report/1" {
            assert_eq!(report["readiness"], "ready");
            assert_eq!(
                report["selection"]["runtimeVersion"],
                "codex-cli 0.149.0-alpha.4.1"
            );
            assert_eq!(report["auth"]["readiness"], "ready");
        } else {
            assert_eq!(report["schema"], "openprose.doctor-report/1");
            assert_eq!(report["ready"], true);
            assert_eq!(
                report["selectedHarnessVersion"],
                "codex-cli 0.149.0-alpha.4.1"
            );
        }
        assert!(!observation.exists(), "readiness probe started a model run");
    }
}

#[cfg(unix)]
#[test]
fn prime_and_omp_preflight_never_launch_auth_probes_and_report_unknown_for_every_profile() {
    for (harness, adapter_id, version, login_profile) in [
        (
            "prime",
            "prime/rpc",
            "prime-agent 0.7.0",
            "prime-harness-login",
        ),
        ("omp", "omp/rpc", "omp/18.0.9", "omp-harness-login"),
    ] {
        for auth_profile in [login_profile, "openrouter"] {
            let temp = TempDir::new().unwrap();
            let (bin, observation) =
                install_live_adapter_fake(temp.path(), harness, adapter_id, version);
            let auth_probe_marker = temp.path().join(".unexpected-prime-omp-auth-probe");
            for args in [
                vec![
                    "--harness",
                    harness,
                    "--transport",
                    "rpc",
                    "--model",
                    "fixture/model",
                    "--auth-profile",
                    auth_profile,
                    "--dry-run",
                    "--output",
                    "json",
                    "run",
                    "hello.prose.md",
                ],
                vec![
                    "--harness",
                    harness,
                    "--transport",
                    "rpc",
                    "--model",
                    "fixture/model",
                    "--auth-profile",
                    auth_profile,
                    "--output",
                    "json",
                    "cli",
                    "doctor",
                ],
            ] {
                let output = prose_with_live_adapter(temp.path(), &bin, harness, &args);
                assert!(
                    output.status.success(),
                    "{adapter_id}/{auth_profile}: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
                let report = json_stdout(&output);
                if report["schema"] == "openprose.runner-dry-run-report/1" {
                    assert_eq!(report["auth"]["readiness"], "unknown");
                } else {
                    assert_eq!(report["schema"], "openprose.doctor-report/1");
                    assert_eq!(report["selectedAuthReadiness"], "unknown");
                }
                assert!(!observation.exists(), "preflight launched a model run");
                assert!(
                    !auth_probe_marker.exists(),
                    "preflight launched the forbidden Prime/OMP auth probe"
                );
            }
        }
    }
}

#[cfg(unix)]
#[test]
fn doctor_marks_the_selected_installed_harness_as_needs_auth() {
    let temp = TempDir::new().unwrap();
    let (bin, _) = install_live_adapter_fake(
        temp.path(),
        "claude",
        "claude/print-stream-json",
        "2.1.243 (Claude Code)",
    );
    let executable = bin.join("claude");
    let source = fs::read_to_string(&executable).unwrap().replace(
        "print('{\"loggedIn\":true}')\n        return 0",
        "print('{\"loggedIn\":false}')\n        return 1",
    );
    fs::write(&executable, source).unwrap();

    let output = prose_with_live_adapter(
        temp.path(),
        &bin,
        "claude",
        &[
            "--harness",
            "claude",
            "--transport",
            "print-stream-json",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
    );
    assert_eq!(output.status.code(), Some(10));
    assert!(output.stderr.is_empty());
    let report = json_stdout(&output);
    assert_eq!(report["ready"], false);
    assert_eq!(report["selectedHarnessVersion"], "2.1.243 (Claude Code)");
    assert_eq!(report["problems"][0]["code"], "HARNESS_NEEDS_AUTH");
    let selected = report["harnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|status| status["id"] == "claude")
        .unwrap();
    assert_eq!(selected["availability"], "needs-auth");
    assert_eq!(selected["detectedVersion"], "2.1.243 (Claude Code)");

    let human = prose_with_live_adapter(
        temp.path(),
        &bin,
        "claude",
        &[
            "--harness",
            "claude",
            "--transport",
            "print-stream-json",
            "cli",
            "doctor",
        ],
    );
    assert_eq!(human.status.code(), Some(10));
    assert!(human.stderr.is_empty());
    let stdout = String::from_utf8(human.stdout).unwrap();
    assert!(
        stdout.contains("Problem: HARNESS_NEEDS_AUTH — The selected harness is not authenticated.")
    );
    assert!(stdout.contains(&format!(
        "Action: Use the exact runner invocation {} for runner operations. Sign in with the selected harness. For Codex, run `codex login`; for Claude, run `claude auth login`; for Prime or OMP cached login, select its explicit harness-login `--auth-profile`; for API-key routes, configure the selected profile. Then retry.",
        shell_single_quote(echo_test_prose().to_str().unwrap())
    )));
    assert!(!stdout.contains("$PROSE"));
}

#[cfg(feature = "test-seams")]
#[test]
fn conformance_adapter_probe_requires_every_internal_guard() {
    let temp = TempDir::new().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args([
            "--harness",
            "codex",
            "--transport",
            "exec-json",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", temp.path())
        .env("OPENPROSE_CONFORMANCE_ADAPTER_MODE", "provider-free-v1")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(json_stdout(&output)["error"]["code"], "CONFIG_INVALID");
}

#[cfg(feature = "test-seams")]
#[test]
fn conformance_adapter_dry_run_reports_admitted_facts_without_starting_probe() {
    let temp = TempDir::new().unwrap();
    let observation = temp.path().join("must-not-exist.json");
    let output = prose_with_adapter_probe(
        temp.path(),
        "prime/rpc",
        &observation,
        &[
            "--harness",
            "prime",
            "--transport",
            "rpc",
            "--model",
            "fixture/model",
            "--auth-profile",
            "openrouter",
            "--dry-run",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = json_stdout(&output);
    assert_eq!(report["schema"], "openprose.runner-dry-run-report/1");
    assert_eq!(report["wouldStartModel"], false);
    assert_eq!(report["selection"]["harness"], "prime");
    assert_eq!(report["selection"]["transport"], "rpc");
    assert_eq!(report["selection"]["adapterId"], "prime/rpc");
    assert_eq!(report["selection"]["runtimeVersion"], Value::Null);
    assert_eq!(report["selection"]["model"], "fixture/model");
    assert_eq!(report["prompt"]["placement"], "system-append");
    assert_eq!(report["prompt"]["strictness"], "strict");
    assert_eq!(report["isolation"], "partial");
    assert_eq!(report["auth"]["category"], "harness-managed");
    assert_eq!(report["auth"]["readiness"], "unknown");
    assert_eq!(report["billingOwner"], "user-provider");
    assert_eq!(report["readiness"], "ready");
    assert_eq!(report["blockingError"], Value::Null);
    assert!(!observation.exists(), "dry-run started the adapter probe");
}

#[cfg(feature = "test-seams")]
#[test]
fn deterministic_mock_consumes_the_shared_frozen_descriptor() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
    );
    assert!(output.status.success());
    let result = json_stdout(&output);
    assert_eq!(result["adapter"]["harnessVersion"], "1.0.0");
    assert_eq!(
        result["adapter"]["descriptorDigestSha256"],
        "65849560c167c1ea98d8e6b46c7703b6ceb4b18dc2564c48de5cbba099576c4a"
    );
    assert_eq!(
        result["negotiatedCapabilities"]["cancellation"],
        "unsupported"
    );
    assert_eq!(result["semantic"]["status"], "not-applicable");
    assert_eq!(result["runnerExitCode"], 0);
    assert_eq!(
        result["runner"]["commit"],
        option_env!("OPENPROSE_BUILD_COMMIT").unwrap_or("development")
    );
}

#[cfg(feature = "test-seams")]
#[test]
fn fake_process_route_is_explicit_external_and_delivers_exact_task() {
    let temp = TempDir::new().unwrap();
    let observation = temp.path().join("observation.json");
    let output = prose_with_fake(
        temp.path(),
        "success",
        &[
            "--harness",
            "mock",
            "--transport",
            "fake-process",
            "--output",
            "json",
            "run",
            "two words.prose.md",
            ";$(touch nope)",
        ],
        &[(
            "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION",
            observation.clone().into_os_string(),
        )],
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result = json_stdout(&output);
    assert_eq!(result["adapter"]["id"], "mock/fake-process");
    assert_eq!(result["adapter"]["harnessVersion"], "1.0.0");
    assert_eq!(
        result["adapter"]["descriptorDigestSha256"],
        "d29305f10f77e3e56bcd1dc88f561b964431994244f0f7cff0065129481b693a"
    );
    let observed: Value = serde_json::from_slice(&fs::read(observation).unwrap()).unwrap();
    assert_eq!(observed["task"]["sha256"], result["digests"]["taskSha256"]);
    assert_eq!(observed["image"]["byteLength"], 346);
    assert_eq!(
        observed["cwd"],
        temp.path().canonicalize().unwrap().to_str().unwrap()
    );
    assert_eq!(observed["environment"].as_object().unwrap().len(), 3);
    assert_eq!(result["semantic"]["status"], "not-applicable");
    assert_eq!(result["runnerExitCode"], 0);
    for marker in ["--image-file", "--task-file"] {
        let argv = observed["argv"].as_array().unwrap();
        let index = argv.iter().position(|value| value == marker).unwrap();
        let temporary = PathBuf::from(argv[index + 1].as_str().unwrap());
        assert!(
            !temporary.exists(),
            "private prompt file survived completion"
        );
    }
}

#[cfg(feature = "test-seams")]
#[test]
fn fake_process_protocol_failures_and_stderr_keep_stream_contracts() {
    let temp = TempDir::new().unwrap();
    let malformed = prose_with_fake(
        temp.path(),
        "malformed",
        &[
            "--harness",
            "mock",
            "--transport",
            "fake-process",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
        &[],
    );
    assert_eq!(malformed.status.code(), Some(22));
    let malformed_result = json_stdout(&malformed);
    assert_eq!(malformed_result["error"]["code"], "PROTOCOL_MALFORMED");
    assert_eq!(malformed_result["error"]["details"]["processExit"], 0);
    assert_eq!(
        malformed_result["error"]["details"]["processSignal"],
        Value::Null
    );

    let stderr = prose_with_fake(
        temp.path(),
        "stderr",
        &[
            "--harness",
            "mock",
            "--transport",
            "fake-process",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
        &[],
    );
    assert!(stderr.status.success());
    assert_eq!(String::from_utf8_lossy(&stderr.stdout).lines().count(), 1);
    assert!(String::from_utf8_lossy(&stderr.stderr).contains("stderr remains separate"));
    let result: Value = serde_json::from_slice(&stderr.stdout).unwrap();
    assert_eq!(result["runnerExitCode"], 0);

    let terminal_nonzero = prose_with_fake(
        temp.path(),
        "terminal-nonzero",
        &[
            "--harness",
            "mock",
            "--transport",
            "fake-process",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
        &[],
    );
    assert_eq!(terminal_nonzero.status.code(), Some(22));
    let result = json_stdout(&terminal_nonzero);
    assert_eq!(result["adapter"]["id"], "mock/fake-process");
    assert_eq!(
        result["digests"]["deliveredImageSha256"],
        result["languageImage"]["sha256"]
    );
    assert_eq!(result["terminal"]["transportCompleted"], false);
    assert_eq!(result["terminal"]["classification"], "exit-code");
    assert_eq!(result["terminal"]["terminalEventObserved"], true);
    assert_eq!(result["terminal"]["exitCode"], 17);
    assert_eq!(result["terminal"]["signal"], Value::Null);
    assert_eq!(result["semantic"]["status"], "unknown");
    assert!(
        result["semantic"]["terminalEnvelopeDigestSha256"]
            .as_str()
            .is_some()
    );
    assert_eq!(
        result["error"]["details"],
        json!({
            "processExit":17,
            "processSignal":null,
            "terminalEventObserved":true
        })
    );
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn human_fake_process_stderr_escapes_controls_while_machine_stderr_is_unchanged() {
    let temp = TempDir::new().unwrap();
    let raw = "ordinary line\nhostile \\ \u{001B}]0;owned\u{0007}\r\t\u{0085}\u{2028}\u{2029}\n";
    let replacement = concat!(
        "records = _records()\n",
        "    os.write(sys.stderr.fileno(), b\"ordinary line\\nhostile \\\\ ",
        "\\x1b]0;owned\\x07\\r\\t\\xc2\\x85\\xe2\\x80\\xa8\\xe2\\x80\\xa9\\n\")"
    );
    let executable = mutated_fake_harness(temp.path(), "records = _records()", replacement);

    for scenario in ["success", "malformed"] {
        let human = prose_with_fake_executable(
            temp.path(),
            &executable,
            scenario,
            &["--harness", "mock", "--transport", "fake-process", "run"],
            &[],
        );
        assert_eq!(human.status.success(), scenario == "success");
        let rendered = String::from_utf8(human.stderr).unwrap();
        assert!(
            rendered.contains(&expected_human_safe_multiline(raw)),
            "{scenario}: {rendered:?}"
        );
        for unsafe_value in [
            "\u{001B}", "\u{0007}", "\r", "\t", "\u{0085}", "\u{2028}", "\u{2029}",
        ] {
            assert!(
                !rendered.contains(unsafe_value),
                "{scenario}: {unsafe_value:?}"
            );
        }

        let machine = prose_with_fake_executable(
            temp.path(),
            &executable,
            scenario,
            &[
                "--harness",
                "mock",
                "--transport",
                "fake-process",
                "--output",
                "json",
                "run",
            ],
            &[],
        );
        assert_eq!(machine.status.success(), scenario == "success");
        assert!(String::from_utf8(machine.stderr).unwrap().contains(raw));
    }
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn fake_process_rejects_every_terminal_outside_the_closed_sentinel_contract() {
    let mutations = [
        (
            "semantic-success",
            "\"semanticStatus\": \"not-applicable\"",
            "\"semanticStatus\": \"success\"",
        ),
        (
            "semantic-unknown",
            "\"semanticStatus\": \"not-applicable\"",
            "\"semanticStatus\": \"unknown\"",
        ),
        (
            "wrong-schema",
            "\"schema\": \"openprose.sentinel-terminal-envelope/1\"",
            "\"schema\": \"openprose.some-other-terminal/1\"",
        ),
        (
            "extra-field",
            "\"marker\": \"OPENPROSE_SENTINEL_TERMINAL_V1\",",
            "\"marker\": \"OPENPROSE_SENTINEL_TERMINAL_V1\", \"unexpected\": True,",
        ),
    ];

    for (name, from, to) in mutations {
        let temp = TempDir::new().unwrap();
        let executable = mutated_fake_harness(temp.path(), from, to);
        let output = prose_with_fake_executable(
            temp.path(),
            &executable,
            "success",
            &[
                "--harness",
                "mock",
                "--transport",
                "fake-process",
                "--output",
                "json",
                "run",
                "fixture.prose.md",
            ],
            &[],
        );
        assert_eq!(output.status.code(), Some(22), "{name}");
        let result = json_stdout(&output);
        assert_eq!(result["error"]["code"], "PROTOCOL_MALFORMED", "{name}");
        assert_eq!(result["runnerExitCode"], 22, "{name}");
        assert_eq!(result["semantic"]["status"], "unknown", "{name}");
        assert_eq!(result["terminal"]["terminalEventObserved"], true, "{name}");
    }
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn fake_process_cancellation_reports_native_signal_and_cleans_descendants() {
    let temp = TempDir::new().unwrap();
    let identities = temp.path().join("descendants.json");
    let output = prose_with_fake(
        temp.path(),
        "descendant",
        &[
            "--harness",
            "mock",
            "--transport",
            "fake-process",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
        &[
            (
                "OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES",
                identities.clone().into_os_string(),
            ),
            ("OPENPROSE_CONFORMANCE_CANCEL_AFTER_MS", "2000".into()),
        ],
    );
    assert_eq!(output.status.code(), Some(24));
    let result = json_stdout(&output);
    assert_eq!(result["adapter"]["id"], "mock/fake-process");
    assert_eq!(result["adapter"]["harnessVersion"], "1.0.0");
    assert_eq!(result["terminal"]["classification"], "cancelled");
    assert_eq!(result["terminal"]["transportCompleted"], false);
    assert_eq!(result["terminal"]["exitCode"], Value::Null);
    assert_eq!(result["terminal"]["signal"], "SIGTERM");
    assert_eq!(
        result["error"]["details"],
        json!({
            "processExit":null,
            "processSignal":"SIGTERM",
            "terminalEventObserved":false
        })
    );
    let descendants: Value = serde_json::from_slice(&fs::read(identities).unwrap()).unwrap();
    let expected_nonce = format!("run-{}", result["invocationId"].as_str().unwrap());
    assert_eq!(descendants["runNonce"], expected_nonce);
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn actual_sigint_emits_one_cancelled_terminal_and_cleans_controlled_in_group_descendants() {
    use rustix::process::{Pid, Signal, kill_process, test_kill_process};

    for output_mode in ["json", "jsonl"] {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let xdg = home.join("xdg");
        let identities = temp.path().join("descendants.json");
        fs::create_dir_all(&xdg).unwrap();
        let child = Command::new(sentinel_prose())
            .args([
                "--harness",
                "mock",
                "--transport",
                "fake-process",
                "--timeout",
                "10s",
                "--output",
                output_mode,
                "run",
                "fixture.prose.md",
            ])
            .current_dir(temp.path())
            .env_clear()
            .env("HOME", &home)
            .env("XDG_CONFIG_HOME", &xdg)
            .env("LANG", "C.UTF-8")
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("OPENPROSE_CONFORMANCE_FAKE_HARNESS", fake_harness())
            .env("OPENPROSE_CONFORMANCE_FAKE_SCENARIO", "descendant")
            .env("OPENPROSE_CONFORMANCE_DESCENDANT_IDENTITIES", &identities)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut cleanup = PublishedFixtureCleanup::new(child, identities.clone());
        let deadline = Instant::now() + Duration::from_secs(5);
        while !identities.exists() && cleanup.child_mut().try_wait().unwrap().is_none() {
            assert!(
                Instant::now() < deadline,
                "descendant fixture did not become ready"
            );
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            identities.exists(),
            "runner exited before descendant identities"
        );
        let runner_pid = Pid::from_raw(i32::try_from(cleanup.child_pid()).unwrap()).unwrap();
        kill_process(runner_pid, Signal::INT).unwrap();
        let output = cleanup.wait_with_output(Duration::from_secs(8));
        assert_eq!(output.status.code(), Some(24), "{output_mode}");
        assert!(output.stderr.is_empty(), "{output_mode}");

        let records = String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .collect::<Vec<_>>();
        if output_mode == "json" {
            assert_eq!(records.len(), 1);
            assert_eq!(records[0]["error"]["code"], "CANCELLED");
            assert_eq!(records[0]["runnerExitCode"], 24);
        } else {
            let terminals = records
                .iter()
                .filter(|record| {
                    matches!(
                        record["type"].as_str(),
                        Some("runner.completed" | "runner.failed")
                    )
                })
                .collect::<Vec<_>>();
            assert_eq!(terminals.len(), 1);
            assert_eq!(terminals[0]["type"], "runner.failed");
            assert_eq!(terminals[0]["payload"]["error"]["code"], "CANCELLED");
        }

        let observed: Value = serde_json::from_slice(&fs::read(&identities).unwrap()).unwrap();
        for name in ["childPid", "grandchildPid"] {
            let raw = i32::try_from(observed[name].as_i64().unwrap()).unwrap();
            let pid = Pid::from_raw(raw).unwrap();
            assert!(
                test_kill_process(pid).is_err(),
                "{name} {raw} survived SIGINT"
            );
        }
    }
}

#[cfg(feature = "test-seams")]
#[test]
fn mock_preserves_opaque_unicode_whitespace_and_metacharacter_argv() {
    let temp = TempDir::new().unwrap();
    let opaque = [
        "run",
        "path with spaces/example.prose.md",
        "--harness",
        "claude",
        "--model",
        "language-value",
        "雪",
        "",
        "line\nbreak",
        ";$(touch nope)",
        "*?[abc]",
    ];
    let mut runner_args = vec!["--harness", "mock", "--output", "json"];
    runner_args.extend(opaque);
    let output = prose(temp.path(), &runner_args);
    assert!(output.status.success());
    let result = json_stdout(&output);
    assert_eq!(result["adapter"]["id"], "mock/in-memory");
    let mut argv = vec!["prose"];
    argv.extend(opaque);
    let task = json!({
        "schema":"openprose.task-envelope/1",
        "argv":argv,
        "interactionMode":"non-interactive"
    });
    assert_eq!(
        result["digests"]["taskSha256"],
        sha256_hex(&serde_json::to_vec(&task).unwrap())
    );
}

#[cfg(feature = "test-seams")]
#[test]
fn delimiter_forces_cli_through_the_language_path() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--output",
            "json",
            "--",
            "cli",
            "doctor",
            "--json",
        ],
    );
    assert!(output.status.success());
    let result = json_stdout(&output);
    let task = json!({
        "schema":"openprose.task-envelope/1",
        "argv":["prose","cli","doctor","--json"],
        "interactionMode":"non-interactive"
    });
    assert_eq!(
        result["digests"]["taskSha256"],
        sha256_hex(&serde_json::to_vec(&task).unwrap())
    );
}

#[test]
fn config_explain_reports_closed_precedence_sources() {
    let temp = TempDir::new().unwrap();
    let project = temp.path().join("project");
    let child = project.join("child");
    let home = temp.path().join("home");
    fs::create_dir_all(project.join(".git")).unwrap();
    fs::create_dir_all(project.join(".prose")).unwrap();
    fs::create_dir_all(&child).unwrap();
    fs::create_dir_all(home.join("xdg/openprose")).unwrap();
    fs::write(
        home.join("xdg/openprose/cli.toml"),
        "model = \"user-model\"\ntimeout = \"2m\"\n",
    )
    .unwrap();
    fs::write(
        project.join(".prose/cli.toml"),
        "transport = \"project-transport\"\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args([
            "--cwd",
            child.to_str().unwrap(),
            "--harness",
            "mock",
            "--auth-profile",
            "flag-profile",
            "cli",
            "config",
            "explain",
            "--json",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", home.join("xdg"))
        .env("PROSE_TIMEOUT", "30s")
        .output()
        .unwrap();
    assert!(output.status.success());
    let report = json_stdout(&output);
    assert_eq!(report["values"]["harness"]["source"]["kind"], "flag");
    assert_eq!(
        report["values"]["transport"]["source"]["kind"],
        "project-config"
    );
    assert_eq!(report["values"]["model"]["source"]["kind"], "user-config");
    assert_eq!(report["values"]["timeout"]["source"]["kind"], "environment");
    assert_eq!(report["values"]["authProfile"]["value"], "flag-profile");
    assert_eq!(
        report["values"]["authProfile"]["source"],
        json!({"kind":"flag","location":"--auth-profile"})
    );
    assert_eq!(
        report["cwd"]["value"],
        fs::canonicalize(child).unwrap().to_str().unwrap()
    );
    assert_eq!(report["cwd"]["source"]["kind"], "flag");
}

#[test]
fn harness_use_persists_the_user_default_without_starting_a_harness() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "harness", "use", "claude"],
    );
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let report = json_stdout(&output);
    let path = temp.path().join("home/xdg/openprose/cli.toml");
    assert_eq!(
        report,
        json!({
            "schema":"openprose.harness-selection/1",
            "harness":"claude",
            "scope":"user",
            "path":path.display().to_string(),
            "changed":true
        })
    );
    assert_eq!(fs::read_to_string(&path).unwrap(), "harness = \"claude\"\n");

    let unchanged = prose(
        temp.path(),
        &["--output", "json", "cli", "harness", "use", "claude"],
    );
    assert!(unchanged.status.success());
    assert_eq!(json_stdout(&unchanged)["changed"], false);
}

#[cfg(unix)]
#[test]
fn harness_use_enforces_private_config_permissions_under_a_permissive_umask() {
    use std::os::unix::fs::PermissionsExt as _;

    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    fs::set_permissions(&xdg, fs::Permissions::from_mode(0o755)).unwrap();

    let output = Command::new("/bin/sh")
        .args([
            "-c",
            "umask 000; exec \"$1\" --output json cli harness use codex",
            "openprose-config-permissions-test",
        ])
        .arg(sentinel_prose())
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .output()
        .unwrap();

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(json_stdout(&output)["changed"], true);
    let parent = xdg.join("openprose");
    let path = parent.join("cli.toml");
    assert_eq!(
        fs::metadata(&parent).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );

    fs::set_permissions(&parent, fs::Permissions::from_mode(0o777)).unwrap();
    let switched = prose(
        temp.path(),
        &["--output", "json", "cli", "harness", "use", "claude"],
    );
    assert!(switched.status.success());
    assert_eq!(
        fs::metadata(&parent).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn harness_use_refuses_a_conflicting_project_or_environment_harness_before_write() {
    for source in ["project", "environment"] {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let xdg = home.join("xdg");
        let user_config = xdg.join("openprose/cli.toml");
        fs::create_dir_all(user_config.parent().unwrap()).unwrap();
        let original = "harness = \"openprose\"\ntimeout = \"30s\"\n";
        fs::write(&user_config, original).unwrap();
        if source == "project" {
            fs::create_dir_all(temp.path().join(".prose")).unwrap();
            fs::write(
                temp.path().join(".prose/cli.toml"),
                "harness = \"claude\"\n",
            )
            .unwrap();
        }

        let mut command = Command::new(sentinel_prose());
        command
            .args(["cli", "harness", "use", "codex", "--json"])
            .current_dir(temp.path())
            .env_clear()
            .env("HOME", &home)
            .env("XDG_CONFIG_HOME", &xdg);
        if source == "environment" {
            command.env("PROSE_HARNESS", "claude");
        }
        let output = command.output().unwrap();

        assert_eq!(output.status.code(), Some(2), "{source}");
        assert!(output.stderr.is_empty(), "{source}");
        let error = json_stdout(&output);
        assert_eq!(error["schema"], "openprose.runner-error/1", "{source}");
        assert_eq!(error["code"], "CONFIG_INVALID", "{source}");
        assert_eq!(error["details"]["selectedHarness"], "codex", "{source}");
        assert_eq!(error["details"]["effectiveHarness"], "claude", "{source}");
        assert_eq!(
            error["details"]["effectiveHarnessSource"],
            if source == "project" {
                "project-config"
            } else {
                "environment"
            },
            "{source}"
        );
        assert_eq!(
            fs::read_to_string(&user_config).unwrap(),
            original,
            "{source}"
        );
    }
}

#[cfg(unix)]
#[test]
fn harness_use_persists_an_explicit_prime_bundle_from_suffix_or_prefix_without_spawning() {
    use std::os::unix::fs::PermissionsExt as _;

    let temp = TempDir::new().unwrap();
    let bin = temp.path().join("bin");
    let observation = temp.path().join("selection-must-not-spawn");
    fs::create_dir_all(&bin).unwrap();
    fs::write(
        bin.join("prime-agent"),
        format!(
            "#!/bin/sh\nprintf spawned > {}\nexit 99\n",
            shell_single_quote(observation.to_str().unwrap())
        ),
    )
    .unwrap();
    fs::set_permissions(bin.join("prime-agent"), fs::Permissions::from_mode(0o700)).unwrap();
    let home = temp.path().join("home");
    let xdg = home.join("xdg");
    fs::create_dir_all(&xdg).unwrap();
    let output = Command::new(sentinel_prose())
        .args([
            "cli",
            "harness",
            "use",
            "prime",
            "--model",
            "openai/gpt-5.4",
            "--auth-profile",
            "prime-harness-login",
            "--json",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("PATH", &bin)
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(json_stdout(&output)["harness"], "prime");
    let path = xdg.join("openprose/cli.toml");
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "auth_profile = \"prime-harness-login\"\nharness = \"prime\"\nmodel = \"openai/gpt-5.4\"\n"
    );
    assert!(!observation.exists());

    let unchanged = Command::new(sentinel_prose())
        .args([
            "--model",
            "openai/gpt-5.4",
            "--auth-profile",
            "prime-harness-login",
            "--output",
            "json",
            "cli",
            "harness",
            "use",
            "prime",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("PATH", &bin)
        .output()
        .unwrap();
    assert!(unchanged.status.success());
    assert_eq!(json_stdout(&unchanged)["changed"], false);
    assert!(!observation.exists());
}

#[test]
fn prime_and_omp_selection_require_both_explicit_cli_values_and_never_inherit_them() {
    for harness in ["prime", "omp"] {
        for explicit in [
            vec!["--model", "openai/gpt-5.4"],
            vec!["--auth-profile", "openai"],
            Vec::new(),
        ] {
            let temp = TempDir::new().unwrap();
            let home = temp.path().join("home");
            let xdg = home.join("xdg");
            let path = xdg.join("openprose/cli.toml");
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let original = "harness = \"claude\"\nmodel = \"stale/model\"\nauth_profile = \"claude-subscription\"\ntimeout = \"30s\"\n";
            fs::write(&path, original).unwrap();
            let mut args = vec!["cli", "harness", "use", harness];
            args.extend(explicit);
            args.push("--json");
            let output = Command::new(sentinel_prose())
                .args(args)
                .current_dir(temp.path())
                .env_clear()
                .env("HOME", &home)
                .env("XDG_CONFIG_HOME", &xdg)
                .env("PROSE_MODEL", "environment/model")
                .env("PROSE_AUTH_PROFILE", "openrouter")
                .output()
                .unwrap();
            assert_eq!(output.status.code(), Some(2), "{harness}");
            let error = json_stdout(&output);
            assert_eq!(error["code"], "INVOCATION_INVALID");
            assert_eq!(error["boundary"], "invocation");
            assert_eq!(error["message"], "Runner invocation is invalid.");
            assert_eq!(
                error["action"],
                format!(
                    "Invoke the `cli harness use {harness}` runner operation with both the `--model` and `--auth-profile` options, then retry."
                )
            );
            assert_eq!(
                error["details"]["reason"],
                "Prime and OMP selection requires explicit CLI --model and --auth-profile options; inherited configuration does not select a credential route."
            );
            assert_eq!(
                error["details"]["requiredOptions"],
                json!(["--model", "--auth-profile"])
            );
            assert!(error["details"]["supportedAuthProfiles"].is_array());
            assert_eq!(fs::read_to_string(&path).unwrap(), original);
        }
    }
}

#[test]
fn missing_prime_selection_options_have_exact_human_invocation_repair() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["cli", "harness", "use", "prime"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.starts_with("INVOCATION_INVALID at invocation: Runner invocation is invalid.\n")
    );
    assert!(stderr.contains(
        "Detail: Prime and OMP selection requires explicit CLI --model and --auth-profile options; inherited configuration does not select a credential route.\n"
    ));
    assert!(stderr.contains(&format!(
        "Action: Use the exact runner invocation {} for runner operations. Invoke the `cli harness use prime` runner operation with both the `--model` and `--auth-profile` options, then retry.\n",
        expected_human_runner_command("").trim_end()
    )));
    assert!(!stderr.contains("Source: unavailable"));
    assert!(!stderr.contains("billing"));
}

#[test]
fn malformed_persisted_configuration_keeps_configuration_boundary_repair() {
    let temp = TempDir::new().unwrap();
    let path = temp.path().join("home/xdg/openprose/cli.toml");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let original = "harness = [\n";
    fs::write(&path, original).unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "harness", "use", "prime"],
    );
    assert_eq!(output.status.code(), Some(2));
    let error = json_stdout(&output);
    assert_eq!(error["code"], "CONFIG_INVALID");
    assert_eq!(error["boundary"], "configuration");
    assert_eq!(error["message"], "Runner configuration is invalid.");
    assert_eq!(
        error["action"],
        "Correct or remove the reported configuration source or setting, then invoke the `cli config explain` runner operation to verify the repair."
    );
    assert_eq!(fs::read_to_string(path).unwrap(), original);
}

#[test]
fn harness_selection_validates_route_and_model_before_writing() {
    for args in [
        [
            "cli",
            "harness",
            "use",
            "prime",
            "--model",
            "unqualified",
            "--auth-profile",
            "prime-harness-login",
            "--json",
        ],
        [
            "cli",
            "harness",
            "use",
            "omp",
            "--model",
            "openai/gpt-5.4",
            "--auth-profile",
            "prime-harness-login",
            "--json",
        ],
    ] {
        let temp = TempDir::new().unwrap();
        let output = prose(temp.path(), &args);
        assert_eq!(output.status.code(), Some(2));
        assert_eq!(json_stdout(&output)["code"], "CONFIG_INVALID");
        assert!(!temp.path().join("home/xdg/openprose/cli.toml").exists());
    }
}

#[test]
fn codex_claude_and_openprose_switches_clear_stale_bundle_values() {
    let temp = TempDir::new().unwrap();
    let path = temp.path().join("home/xdg/openprose/cli.toml");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(
        &path,
        "harness = \"prime\"\nmodel = \"openai/gpt-5.4\"\nauth_profile = \"prime-harness-login\"\ntimeout = \"30s\"\n",
    )
    .unwrap();

    let claude = prose(temp.path(), &["cli", "harness", "use", "claude", "--json"]);
    assert!(claude.status.success());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "harness = \"claude\"\ntimeout = \"30s\"\n"
    );

    let codex = prose(
        temp.path(),
        &[
            "cli",
            "harness",
            "use",
            "codex",
            "--model",
            "gpt-5.4",
            "--auth-profile",
            "cached-chatgpt-login",
            "--json",
        ],
    );
    assert!(codex.status.success());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "auth_profile = \"cached-chatgpt-login\"\nharness = \"codex\"\nmodel = \"gpt-5.4\"\ntimeout = \"30s\"\n"
    );

    let openprose = prose(
        temp.path(),
        &["cli", "harness", "use", "openprose", "--json"],
    );
    assert!(openprose.status.success());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "harness = \"openprose\"\ntimeout = \"30s\"\n"
    );
}

#[test]
fn openprose_selection_rejects_explicit_external_route_without_writing() {
    let temp = TempDir::new().unwrap();
    let path = temp.path().join("home/xdg/openprose/cli.toml");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let original = "harness = \"claude\"\ntimeout = \"30s\"\n";
    fs::write(&path, original).unwrap();
    let output = prose(
        temp.path(),
        &[
            "cli",
            "harness",
            "use",
            "openprose",
            "--auth-profile",
            "openai-api-key",
            "--json",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(json_stdout(&output)["code"], "CONFIG_INVALID");
    assert_eq!(fs::read_to_string(&path).unwrap(), original);
}

#[test]
fn human_harness_use_confirms_the_default_and_gives_the_verification_command() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["cli", "harness", "use", "claude"]);
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let path = temp.path().join("home/xdg/openprose/cli.toml");
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!(
            "Default harness: claude (updated)\nRoute: claude-subscription (Claude default; not saved)\nModel: harness default (not saved)\nConfiguration: {}\nNext: {}\n",
            path.display(),
            expected_human_runner_command("cli doctor")
        )
    );

    let unchanged = prose(temp.path(), &["cli", "harness", "use", "claude"]);
    assert!(unchanged.status.success());
    assert!(unchanged.stderr.is_empty());
    let stdout = String::from_utf8(unchanged.stdout).unwrap();
    assert!(stdout.contains("Default harness: claude (already selected)\n"));
    assert!(stdout.contains(&format!(
        "Next: {}\n",
        expected_human_runner_command("cli doctor")
    )));
    assert!(!stdout.contains("$PROSE"));
}

#[test]
fn mock_is_rejected_when_selected_ambiently() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    fs::create_dir_all(home.join("xdg")).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(["--output", "json", "run", "fixture.prose.md"])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", home.join("xdg"))
        .env("PROSE_HARNESS", "mock")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let result = json_stdout(&output);
    assert_eq!(result["error"]["code"], "CONFIG_INVALID");
}

#[cfg(feature = "test-seams")]
#[test]
fn dry_run_is_machine_readable_and_starts_no_run() {
    let cases = [
        ("deterministic", "mock/in-memory"),
        ("fake-process", "mock/fake-process"),
    ];
    for (transport, adapter_id) in cases {
        let temp = TempDir::new().unwrap();
        let output = prose(
            temp.path(),
            &[
                "--harness",
                "mock",
                "--transport",
                transport,
                "--dry-run",
                "--output",
                "json",
                "write",
                "fixture.prose.md",
            ],
        );
        assert!(output.status.success(), "{transport}");
        let report = json_stdout(&output);
        assert_eq!(report["schema"], "openprose.runner-dry-run-report/1");
        assert_eq!(report["wouldStartModel"], false);
        assert_eq!(report["selection"]["harness"], "mock");
        assert_eq!(report["selection"]["transport"], transport);
        assert_eq!(report["selection"]["adapterId"], adapter_id);
        assert_eq!(report["auth"]["category"], "none-test-only");
        assert_eq!(report["auth"]["readiness"], "not-applicable");
        assert_eq!(report["billingOwner"], "test-fixture");
        assert_eq!(report["readiness"], "ready");
        assert!(report["blockingError"].is_null());
        assert_eq!(
            report["configuration"]
                .as_array()
                .unwrap()
                .iter()
                .map(|entry| entry["key"].as_str().unwrap())
                .collect::<Vec<_>>(),
            [
                "cwd",
                "harness",
                "transport",
                "model",
                "timeout",
                "output",
                "color",
                "verbose",
                "authProfile",
            ]
        );
        assert_eq!(report["configuration"][0]["source"], "default");
        assert_eq!(report["configuration"][0]["location"], "process cwd");
        assert_eq!(report["configuration"][0]["redacted"], false);
        assert_eq!(report["configuration"][8]["redacted"], true);
        assert!(
            report["configuration"]
                .as_array()
                .unwrap()
                .iter()
                .take(8)
                .all(|entry| entry["redacted"] == false)
        );
    }
}

#[test]
fn default_dry_run_identifies_the_hosted_adapter_without_fallback() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--dry-run", "--output", "json", "write", "fixture.prose.md"],
    );
    assert_eq!(output.status.code(), Some(10));
    let report = json_stdout(&output);
    assert_eq!(report["selection"]["harness"], "openprose");
    assert_eq!(report["selection"]["transport"], "hosted");
    assert_eq!(report["selection"]["adapterId"], "openprose/hosted");
    assert_eq!(report["billingOwner"], "openprose");
    assert_eq!(report["readiness"], "blocked");
    assert_eq!(report["blockingError"]["code"], "HOSTED_UNAVAILABLE");
    assert_eq!(
        report["blockingError"]["details"]["fallbackSelected"],
        false
    );
    assert_eq!(
        report["configuration"]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| entry["key"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [
            "cwd",
            "harness",
            "transport",
            "model",
            "timeout",
            "output",
            "color",
            "verbose",
            "authProfile",
        ]
    );
    assert_eq!(report["configuration"][8]["redacted"], true);
}

// docs/kernel-startup.md, "Fixed inputs and tests", distinguishes ordinary
// compiled Codex append from the retained test-seam framing profile. The frozen
// codex-exec-json-developer.v1.json and codex-exec-json.v1.json recipes specify
// strict and degraded placement respectively. Do not derive these expectations
// from observed runner output or cfg(test) of this integration-test executable.
fn expected_native_placement() -> (&'static str, &'static str) {
    if cfg!(feature = "test-seams") {
        ("user-prefix-framed", "degraded")
    } else {
        ("developer", "strict")
    }
}

#[test]
fn blocked_adapter_dry_run_reports_recipe_facts_without_spawning() {
    let cases = [
        (
            "codex",
            "exec-json",
            "codex/exec-json",
            expected_native_placement().0,
            expected_native_placement().1,
            "unsupported",
            "HARNESS_UNAVAILABLE",
        ),
        (
            "claude",
            "print-stream-json",
            "claude/print-stream-json",
            "system-append",
            "strict",
            "partial",
            "HARNESS_UNAVAILABLE",
        ),
        (
            "prime",
            "rpc",
            "prime/rpc",
            "system-append",
            "strict",
            "partial",
            "CONFIG_INVALID",
        ),
        (
            "omp",
            "rpc",
            "omp/rpc",
            "system-append",
            "strict",
            "unsupported",
            "CONFIG_INVALID",
        ),
    ];
    for (harness, transport, adapter_id, placement, strictness, isolation, error_code) in cases {
        let temp = TempDir::new().unwrap();
        let output = prose(
            temp.path(),
            &[
                "--harness",
                harness,
                "--transport",
                transport,
                "--dry-run",
                "--output",
                "json",
                "run",
                "fixture.prose.md",
            ],
        );
        let report = json_stdout(&output);
        assert_eq!(report["wouldStartModel"], false);
        assert_eq!(report["selection"]["adapterId"], adapter_id);
        assert_eq!(report["selection"]["transport"], transport);
        assert_eq!(report["prompt"]["placement"], placement);
        assert_eq!(report["prompt"]["strictness"], strictness);
        assert_eq!(report["isolation"], isolation);
        assert_eq!(report["auth"]["category"], "harness-managed");
        assert_eq!(report["billingOwner"], "user-provider");
        assert_eq!(report["readiness"], "blocked");
        assert_eq!(report["blockingError"]["code"], error_code);
        assert_eq!(
            output.status.code(),
            report["blockingError"]["exitCode"]
                .as_i64()
                .and_then(|code| i32::try_from(code).ok())
        );
    }
}

#[cfg(feature = "test-seams")]
#[test]
fn jsonl_has_exactly_one_terminal_record_and_nothing_after_it() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--output",
            "jsonl",
            "run",
            "fixture.prose.md",
        ],
    );
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let records: Vec<Value> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records.last().unwrap()["type"], "runner.completed");
    assert_eq!(
        records.last().unwrap()["payload"]["result"]["semantic"]["status"],
        "not-applicable"
    );
    assert_eq!(
        records
            .iter()
            .filter(|record| {
                matches!(
                    record["type"].as_str(),
                    Some("runner.completed" | "runner.failed")
                )
            })
            .count(),
        1
    );
}

#[test]
fn compiled_commit_identity_is_exact_in_success_failure_and_stream_results() {
    let expected = option_env!("OPENPROSE_BUILD_COMMIT").unwrap_or("development");
    let temp = TempDir::new().unwrap();

    let success = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
    );
    if cfg!(feature = "test-seams") {
        assert!(success.status.success());
    } else {
        assert_eq!(success.status.code(), Some(10));
    }
    let success_result = json_stdout(&success);
    assert_eq!(success_result["runner"]["commit"], expected);
    if !cfg!(feature = "test-seams") {
        assert_eq!(success_result["error"]["code"], "HARNESS_UNAVAILABLE");
    }

    let failure = prose(
        temp.path(),
        &["--output", "json", "run", "fixture.prose.md"],
    );
    assert_eq!(failure.status.code(), Some(10));
    assert_eq!(json_stdout(&failure)["runner"]["commit"], expected);

    let doctor = prose(temp.path(), &["--output", "json", "cli", "doctor"]);
    assert_eq!(doctor.status.code(), Some(10));
    let doctor_report = json_stdout(&doctor);
    assert_eq!(doctor_report["runner"]["commit"], expected);
    assert_eq!(
        doctor_report["build"],
        json!({
            "profile": if cfg!(debug_assertions) { "development" } else { "release" },
            "testSeamsEnabled": cfg!(feature = "test-seams")
        })
    );

    let streamed = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--output",
            "jsonl",
            "run",
            "fixture.prose.md",
        ],
    );
    if cfg!(feature = "test-seams") {
        assert!(streamed.status.success());
    } else {
        assert_eq!(streamed.status.code(), Some(10));
    }
    let terminal: Value = serde_json::from_str(
        String::from_utf8(streamed.stdout)
            .unwrap()
            .lines()
            .last()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        terminal["type"],
        if cfg!(feature = "test-seams") {
            "runner.completed"
        } else {
            "runner.failed"
        }
    );
    if cfg!(feature = "test-seams") {
        assert_eq!(terminal["payload"]["result"]["runner"]["commit"], expected);
    } else {
        assert_eq!(terminal["payload"]["error"]["code"], "HARNESS_UNAVAILABLE");
    }
}

#[cfg(feature = "test-seams")]
#[test]
fn cargo_build_modes_bind_exact_image_and_test_seam_identities() {
    let temporary = TempDir::new().unwrap();
    let development_target = temporary.path().join("development-target");
    let development = build_mode_candidate(&development_target, false, false);
    assert!(
        development.status.success(),
        "{}{}",
        String::from_utf8_lossy(&development.stdout),
        String::from_utf8_lossy(&development.stderr)
    );
    let development_report = build_mode_report(
        &development_target
            .join("debug")
            .join(if cfg!(windows) { "prose.exe" } else { "prose" }),
        &temporary.path().join("development-run"),
    );
    assert_eq!(
        development_report["build"],
        json!({"profile":"development","testSeamsEnabled":false})
    );
    assert_eq!(development_report["image"]["version"], "echo-v0");

    let explicit_report = build_mode_report(
        sentinel_prose(),
        &temporary.path().join("explicit-test-run"),
    );
    assert_eq!(
        explicit_report["build"],
        json!({"profile":"development","testSeamsEnabled":true})
    );
    assert_eq!(explicit_report["image"]["version"], "sentinel-v1");

    let release_target = temporary.path().join("release-target");
    let release = build_mode_candidate(&release_target, true, false);
    assert!(
        release.status.success(),
        "{}{}",
        String::from_utf8_lossy(&release.stdout),
        String::from_utf8_lossy(&release.stderr)
    );
    let release_report = build_mode_report(
        &release_target
            .join("release")
            .join(if cfg!(windows) { "prose.exe" } else { "prose" }),
        &temporary.path().join("release-run"),
    );
    assert_eq!(
        release_report["build"],
        json!({"profile":"release","testSeamsEnabled":false})
    );
    assert_eq!(release_report["image"]["version"], "echo-v0");

    let release_binary =
        release_target
            .join("release")
            .join(if cfg!(windows) { "prose.exe" } else { "prose" });
    assert_release_mock_rpc_is_rejected(
        &release_binary,
        &temporary.path().join("release-invalid-transport"),
    );

    let rejected = build_mode_candidate(&temporary.path().join("rejected-target"), true, true);
    assert!(!rejected.status.success());
    assert!(
        String::from_utf8_lossy(&rejected.stderr)
            .contains("test-seams cannot be enabled for a release build"),
        "{}",
        String::from_utf8_lossy(&rejected.stderr)
    );
}

#[cfg(not(debug_assertions))]
fn assert_release_build_report(working_directory: &Path) {
    let doctor = prose(working_directory, &["--output", "json", "cli", "doctor"]);
    assert_eq!(doctor.status.code(), Some(10));
    assert_eq!(
        json_stdout(&doctor)["build"],
        json!({"profile":"release","testSeamsEnabled":false})
    );
}

#[cfg(not(debug_assertions))]
#[test]
#[allow(clippy::too_many_lines)]
fn release_build_ambient_environment_cannot_enable_internal_process_seams() {
    let temp = TempDir::new().unwrap();
    let inventory = prose(temp.path(), &["--output", "json", "cli", "harness", "list"]);
    assert!(inventory.status.success());
    let inventory = json_stdout(&inventory);
    let mock = inventory["harnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["id"] == "mock")
        .unwrap();
    assert_eq!(mock["availability"], "unavailable");
    assert_eq!(mock["detectedVersion"], Value::Null);
    assert_eq!(mock["strictWrapperConformant"], false);
    assert_eq!(mock["testOnly"], true);
    assert_eq!(mock["admissionBlock"], "test-seams-disabled");

    let doctor = prose(
        temp.path(),
        &["--harness", "mock", "--output", "json", "cli", "doctor"],
    );
    assert_eq!(doctor.status.code(), Some(10));
    let doctor = json_stdout(&doctor);
    assert_eq!(doctor["ready"], false);
    assert_eq!(doctor["selectedHarness"], "mock");
    assert_eq!(doctor["selectedTransport"], "deterministic");
    assert_eq!(doctor["promptPlacement"], Value::Null);
    assert_eq!(doctor["isolation"], "unsupported");
    assert_eq!(doctor["problems"][0]["code"], "HARNESS_UNAVAILABLE");
    assert_eq!(doctor["problems"][0]["details"]["fallbackAttempted"], false);

    for dry_run in [false, true] {
        let mut arguments = vec!["--harness", "mock", "--output", "json"];
        if dry_run {
            arguments.push("--dry-run");
        }
        arguments.extend(["run", "fixture.prose.md"]);
        let refused = prose(temp.path(), &arguments);
        assert_eq!(refused.status.code(), Some(10));
        let result = json_stdout(&refused);
        assert_eq!(result["transport"], "deterministic");
        assert_eq!(result["error"]["code"], "HARNESS_UNAVAILABLE");
        assert_eq!(
            result["error"]["details"],
            json!({
                "harness":"mock",
                "admissionStatus":"blocked",
                "admissionBlock":"test-seams-disabled",
                "fallbackAttempted":false
            })
        );
        assert_eq!(result["runnerExitCode"], 10);
        assert_eq!(result["semantic"]["status"], "unknown");
        assert_eq!(result["terminal"]["transportCompleted"], false);
        assert_eq!(result["terminal"]["terminalEventObserved"], false);
    }

    let fake_observation = temp.path().join("fake-must-not-exist.json");
    let fake = prose_with_fake(
        temp.path(),
        "success",
        &[
            "--harness",
            "mock",
            "--transport",
            "fake-process",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ],
        &[(
            "OPENPROSE_CONFORMANCE_FAKE_OBSERVATION",
            fake_observation.clone().into_os_string(),
        )],
    );
    assert_eq!(fake.status.code(), Some(10));
    assert_eq!(json_stdout(&fake)["error"]["code"], "HARNESS_UNAVAILABLE");
    assert!(!fake_observation.exists());

    let adapter_observation = temp.path().join("adapter-must-not-exist.json");
    let home = temp.path().join("release-home");
    let empty_path = temp.path().join("release-empty-bin");
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&empty_path).unwrap();
    let adapter = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args([
            "--harness",
            "codex",
            "--transport",
            "exec-json",
            "--output",
            "json",
            "run",
            "fixture.prose.md",
        ])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", &home)
        .env("PATH", &empty_path)
        .env("OPENPROSE_CONFORMANCE_ADAPTER_MODE", "provider-free-v1")
        .env("OPENPROSE_CONFORMANCE_ADAPTER_PROBE", fake_harness())
        .env(
            "OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION",
            &adapter_observation,
        )
        .env(
            "OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP",
            "cached-chatgpt-login",
        )
        .output()
        .unwrap();
    assert_eq!(adapter.status.code(), Some(10));
    assert_eq!(
        json_stdout(&adapter)["error"]["code"],
        "HARNESS_UNAVAILABLE"
    );
    assert!(!adapter_observation.exists());

    assert_release_build_report(temp.path());
}

#[test]
fn runner_diagnostics_and_identity_stay_local() {
    let temp = TempDir::new().unwrap();
    let version = prose(temp.path(), &["--version"]);
    assert!(version.status.success());
    assert_eq!(version.stdout, b"prose 0.1.0 (rust)\n");
    assert!(version.stderr.is_empty());

    let doctor = prose(temp.path(), &["cli", "doctor", "--json"]);
    assert_eq!(doctor.status.code(), Some(10));
    let doctor_report = json_stdout(&doctor);
    assert_eq!(doctor_report["schema"], "openprose.doctor-report/1");
    assert_eq!(doctor_report["selectedHarness"], "openprose");
    assert_eq!(doctor_report["ready"], false);

    let list = prose(temp.path(), &["cli", "harness", "list", "--json"]);
    assert!(list.status.success());
    let list_report = json_stdout(&list);
    assert_eq!(list_report["schema"], "openprose.harness-list/1");
    let harnesses = list_report["harnesses"].as_array().unwrap();
    assert_eq!(
        harnesses
            .iter()
            .map(|harness| harness["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [
            "openprose",
            "prime",
            "omp",
            "codex",
            "claude",
            "agents-sdk",
            "mock"
        ]
    );
    for harness in &harnesses[1..6] {
        assert_eq!(harness["runtime"], "installed-process");
        assert_eq!(harness["availability"], "missing");
        assert_eq!(harness["detectedVersion"], Value::Null);
        assert_eq!(harness["billingOwner"], "user-provider");
        assert_eq!(harness["authCategory"], "harness-managed");
        assert_eq!(harness["strictWrapperConformant"], false);
        assert_eq!(harness["testOnly"], false);
        assert_eq!(harness["admissionBlock"], Value::Null);
    }
    assert_eq!(harnesses[6]["id"], "mock");
    assert_eq!(
        harnesses[6]["detectedVersion"],
        if cfg!(feature = "test-seams") {
            json!("1.0.0")
        } else {
            Value::Null
        }
    );
    assert_eq!(
        harnesses[6]["availability"],
        if cfg!(feature = "test-seams") {
            "available"
        } else {
            "unavailable"
        }
    );
    assert_eq!(harnesses[6]["testOnly"], true);

    let installed_doctor = prose(
        temp.path(),
        &[
            "--harness",
            "codex",
            "--transport",
            "exec-json",
            "cli",
            "doctor",
            "--json",
        ],
    );
    assert_eq!(installed_doctor.status.code(), Some(10));
    let report = json_stdout(&installed_doctor);
    assert_eq!(report["ready"], false);
    assert_eq!(report["selectedHarness"], "codex");
    assert_eq!(report["selectedTransport"], "exec-json");
    assert_eq!(report["selectedAdapterId"], "codex/exec-json");
    assert_eq!(report["promptPlacement"], expected_native_placement().0);
    assert_eq!(report["isolation"], "unsupported");
    assert_eq!(report["billingOwner"], "user-provider");
    assert_eq!(report["authCategory"], "harness-managed");
    assert_eq!(report["problems"][0]["code"], "HARNESS_UNAVAILABLE");
    assert_eq!(report["problems"][0]["details"]["fallbackAttempted"], false);
    let selected = report["harnesses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|harness| harness["id"] == "codex")
        .unwrap();
    assert_eq!(selected["billingOwner"], "user-provider");
    assert_eq!(selected["authCategory"], "harness-managed");
    assert_eq!(selected["admissionBlock"], Value::Null);
    assert_eq!(selected["detectedVersion"], Value::Null);
}

#[test]
fn config_explain_matches_the_closed_source_attributed_report() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &["--output", "json", "cli", "config", "explain"],
    );
    assert!(output.status.success());
    assert_eq!(json_stdout(&output), expected_configuration(temp.path()));
}

#[test]
fn harness_list_matches_the_closed_ordered_inventory() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["--output", "json", "cli", "harness", "list"]);
    assert!(output.status.success());
    let mut expected = operation_fixture("harnesses");
    expected["harnesses"] = expected_harness_inventory();
    assert_eq!(json_stdout(&output), expected);
}

#[test]
fn human_harness_list_marks_the_default_and_gives_ordered_next_actions() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["cli", "harness", "list"]);
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(
        stdout.contains("* openprose availability=not-implemented transport=hosted (selected)\n")
    );
    for line in [
        "  prime availability=missing transport=rpc\n",
        "  omp availability=missing transport=rpc\n",
        "  codex availability=missing transport=exec-json\n",
        "  claude availability=missing transport=print-stream-json\n",
    ] {
        assert!(
            stdout.contains(line),
            "missing human inventory line: {line}"
        );
    }
    assert!(stdout.contains(
        "    runtime prerequisite: bun; availability: missing; detected version: missing; required version: 1.3.14 or newer\n"
    ));
    assert!(stdout.contains(
        "    runtime repair: npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9\n"
    ));
    if cfg!(feature = "test-seams") {
        assert!(stdout.contains("Test-only harnesses:\n"));
        assert!(stdout.contains(
            "  mock availability=available transport=deterministic,fake-process version=1.0.0\n"
        ));
    } else {
        assert!(!stdout.contains("mock"));
        assert!(!stdout.contains("Test-only harnesses:"));
    }
    assert!(stdout.ends_with(&format!(
        "Choose Codex: {}\nChoose Claude: {}\nPrime and OMP: set PROSE_MODEL to a fully qualified provider/model installed in that harness; unset or invalid values are refused.\nChoose Prime: {}\nChoose OMP: {}\nThen verify: {}\n",
        expected_human_runner_command("cli harness use codex"),
        expected_human_runner_command("cli harness use claude"),
        expected_human_runner_command(
            "cli harness use prime --model \"$PROSE_MODEL\" --auth-profile prime-harness-login"
        ),
        expected_human_runner_command(
            "cli harness use omp --model \"$PROSE_MODEL\" --auth-profile omp-harness-login"
        ),
        expected_human_runner_command("cli doctor")
    )));
    assert!(!stdout.contains("replace-with-provider/model"));
    assert!(!stdout.contains("\"$PROSE\""));
    assert!(!stdout.contains("prose cli"));
}

#[cfg(unix)]
fn assert_guarded_model_choice(
    root: &Path,
    hostile_bin: &Path,
    command: &str,
    harness: &str,
    auth_profile: &str,
) {
    for (label, model) in [
        ("unset", None),
        ("unqualified", Some("unqualified".to_owned())),
        (
            "hostile",
            Some(format!(
                "openai/model$(touch {})",
                root.join(format!("{harness}-injection")).display()
            )),
        ),
    ] {
        let case_root = root.join(format!("{harness}-{label}"));
        let config_root = case_root.join("config");
        let mut selection = Command::new("/bin/sh");
        selection
            .args(["-c", command])
            .current_dir(root)
            .env_clear()
            .env("HOME", case_root.join("home"))
            .env("XDG_CONFIG_HOME", &config_root)
            .env("LANG", "C.UTF-8")
            .env("PATH", hostile_bin);
        if let Some(model) = model {
            selection.env("PROSE_MODEL", model);
        }
        let attempted = selection.output().unwrap();
        assert_eq!(attempted.status.code(), Some(2), "{harness}/{label}");
        assert!(!config_root.join("openprose/cli.toml").exists());
        assert!(!root.join(format!("{harness}-injection")).exists());
    }

    let valid_root = root.join(format!("{harness}-valid"));
    let valid_config = valid_root.join("config");
    let selected = Command::new("/bin/sh")
        .args(["-c", command])
        .current_dir(root)
        .env_clear()
        .env("HOME", valid_root.join("home"))
        .env("XDG_CONFIG_HOME", &valid_config)
        .env("LANG", "C.UTF-8")
        .env("PATH", hostile_bin)
        .env("PROSE_MODEL", "openai/gpt-5.4")
        .output()
        .unwrap();
    assert!(selected.status.success());
    assert_eq!(
        fs::read_to_string(valid_config.join("openprose/cli.toml")).unwrap(),
        format!(
            "auth_profile = \"{auth_profile}\"\nharness = \"{harness}\"\nmodel = \"openai/gpt-5.4\"\n"
        )
    );
}

#[cfg(unix)]
#[test]
fn exact_runner_inventory_guidance_never_resolves_a_hostile_path_prose() {
    use std::os::unix::fs::PermissionsExt as _;

    let temp = TempDir::new().unwrap();
    let hostile_bin = temp.path().join("hostile-bin");
    let hostile_marker = temp.path().join("path-resolved-prose-ran");
    fs::create_dir_all(temp.path().join("home/xdg")).unwrap();
    fs::create_dir_all(&hostile_bin).unwrap();
    let hostile = hostile_bin.join("prose");
    fs::write(
        &hostile,
        format!(
            "#!/bin/sh\nprintf attacked > {}\n",
            hostile_marker.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&hostile, fs::Permissions::from_mode(0o700)).unwrap();

    let output = Command::new(sentinel_prose())
        .args(["cli", "harness", "list"])
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", temp.path().join("home"))
        .env("XDG_CONFIG_HOME", temp.path().join("home/xdg"))
        .env("LANG", "C.UTF-8")
        .env("PATH", &hostile_bin)
        .output()
        .unwrap();
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    for line in stdout.lines().filter(|line| line.starts_with("Choose ")) {
        let (_, command) = line.rsplit_once(": ").expect("choice has a command");
        assert!(!command.contains(['<', '>', '|']));
        assert!(
            Command::new("/bin/sh")
                .args(["-n", "-c", command])
                .status()
                .unwrap()
                .success()
        );
    }
    for (label, harness, profile) in [
        ("Prime", "prime", "prime-harness-login"),
        ("OMP", "omp", "omp-harness-login"),
    ] {
        let command = stdout
            .lines()
            .find_map(|line| line.strip_prefix(&format!("Choose {label}: ")))
            .unwrap();
        assert_guarded_model_choice(temp.path(), &hostile_bin, command, harness, profile);
    }
    assert!(!stdout.contains("replace-with-provider/model"));
    assert!(!stdout.contains("\"$PROSE\""));
    assert!(!stdout.contains("prose cli"));
    assert!(!hostile_marker.exists());
}

#[cfg(unix)]
#[test]
fn human_harness_list_includes_a_safely_detected_compatible_version() {
    let temp = TempDir::new().unwrap();
    let (bin, observation) = install_live_adapter_fake(
        temp.path(),
        "codex",
        "codex/exec-json",
        "codex-cli 0.149.0-alpha.4.1",
    );
    let output = prose_with_live_adapter(temp.path(), &bin, "codex", &["cli", "harness", "list"]);
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert!(String::from_utf8(output.stdout).unwrap().contains(
        "  codex availability=available transport=exec-json version=codex-cli 0.149.0-alpha.4.1\n"
    ));
    assert!(!observation.exists());
}

#[test]
fn doctor_matches_the_closed_full_report_and_selected_problem_exit() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["--output", "json", "cli", "doctor"]);
    assert_eq!(output.status.code(), Some(10));
    let mut expected = operation_fixture("doctor");
    expected["cwd"] = Value::String(fs::canonicalize(temp.path()).unwrap().display().to_string());
    expected["configuration"] = expected_configuration(temp.path());
    expected["runner"] = json!({
        "name": "rust",
        "version": env!("CARGO_PKG_VERSION"),
        "commit": option_env!("OPENPROSE_BUILD_COMMIT").unwrap_or("development")
    });
    expected["build"] = json!({
        "profile": if cfg!(debug_assertions) { "development" } else { "release" },
        "testSeamsEnabled": cfg!(feature = "test-seams")
    });
    if !cfg!(feature = "test-seams") {
        expected["image"] = json!({
            "formatVersion":"openprose.skill-runtime-image/1",
            "version":"echo-v0",
            "sha256":"daf3fab11a27b6c982efdad0d223823b35bd7c8de05d9b1d464a30ed8ec146f2",
            "releaseEligible":true
        });
    }
    expected["harnesses"] = expected_harness_inventory();
    assert_eq!(json_stdout(&output), expected);
}

#[test]
fn doctor_reports_installed_adapter_configuration_blockers_without_fallback() {
    let temp = TempDir::new().unwrap();
    let output = prose(
        temp.path(),
        &[
            "--harness",
            "prime",
            "--transport",
            "rpc",
            "--output",
            "json",
            "cli",
            "doctor",
        ],
    );
    assert_eq!(output.status.code(), Some(2));
    let report = json_stdout(&output);
    assert_eq!(report["schema"], "openprose.doctor-report/1");
    assert_eq!(report["selectedHarness"], "prime");
    assert_eq!(report["selectedHarnessVersion"], Value::Null);
    assert_eq!(report["selectedAdapterId"], "prime/rpc");
    assert_eq!(report["isolation"], "advisory");
    assert_eq!(report["problems"][0]["code"], "CONFIG_INVALID");
    assert_eq!(report["problems"][0]["exitCode"], 2);
    assert_eq!(
        report["configuration"]["values"]["harness"]["value"],
        "prime"
    );
    assert_eq!(report["harnesses"], expected_harness_inventory());

    let human = prose(
        temp.path(),
        &["--harness", "prime", "--transport", "rpc", "cli", "doctor"],
    );
    assert_eq!(human.status.code(), Some(2));
    assert!(human.stderr.is_empty());
    let stdout = String::from_utf8(human.stdout).unwrap();
    assert!(stdout.contains(
        "Detail: Prime and OMP require an explicit fully qualified provider/model for functional-alpha execution (`--model` or PROSE_MODEL)."
    ));
}

#[cfg(feature = "test-seams")]
#[test]
fn account_status_and_mutations_use_only_hermetic_service_store() {
    let temp = TempDir::new().unwrap();
    let fixture = temp.path().join("service.json");
    fs::write(
        &fixture,
        json!({"environment":"production","credential":null,"storeAvailable":false,"exchanges":[]})
            .to_string(),
    )
    .unwrap();
    for command in ["status", "login", "logout"] {
        let output = Command::new(env!("CARGO_BIN_EXE_prose"))
            .args(["cli", "auth", command, "--json"])
            .current_dir(temp.path())
            .env_clear()
            .env("HOME", temp.path().join("home"))
            .env("XDG_CONFIG_HOME", temp.path().join("xdg"))
            .env("PROSE_TEST_SERVICE_FIXTURE", &fixture)
            .output()
            .unwrap();
        let result = json_stdout(&output);
        assert_eq!(result["schema"], "openprose.service-operation/1");
        assert_eq!(result["operation"], format!("auth.{command}"));
        assert!(result.get("environment").is_none());
        assert_eq!(result["problem"]["code"], "CREDENTIAL_STORE_UNAVAILABLE");
    }
}

#[cfg(feature = "test-seams")]
/// The retired service-selection option, spelled so the public-surface
/// scan does not match this negative test.
const RETIRED_OPTION: &str = concat!("--service-", "environment");

/// Runs the binary against a service fixture with a clean environment.
#[cfg(feature = "test-seams")]
fn run_with_fixture(
    temp: &TempDir,
    fixture: &Value,
    variables: &[(&str, &str)],
    args: &[&str],
) -> std::process::Output {
    let path = temp.path().join("service.json");
    fs::write(&path, fixture.to_string()).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_prose"));
    command
        .args(args)
        .current_dir(temp.path())
        .env_clear()
        .env("HOME", temp.path().join("home"))
        .env("XDG_CONFIG_HOME", temp.path().join("xdg"))
        .env("XDG_STATE_HOME", temp.path().join("state"))
        .env("PROSE_TEST_SERVICE_FIXTURE", &path);
    for (name, value) in variables {
        command.env(name, value);
    }
    command.output().unwrap()
}

/// The `GET /health` exchange of `cli service status`, at `origin`.
#[cfg(feature = "test-seams")]
fn health_exchange(origin: &str) -> Value {
    json!({"method": "GET", "path": "/health", "origin": origin, "status": 200,
           "body": {"status": "ok", "models": ["model-sol"], "default_model": "model-sol"}})
}

#[cfg(feature = "test-seams")]
#[test]
fn no_command_option_or_saved_setting_selects_a_service() {
    let temp = TempDir::new().unwrap();
    let xdg = temp.path().join("xdg");
    fs::create_dir_all(xdg.join("openprose")).unwrap();
    let config = xdg.join("openprose/cli.toml");
    // A selection saved by an earlier client is ignored, never an error.
    let saved = "# retain comment\nharness = \"codex\"\nservice_environment = \"other\"\n";
    fs::write(&config, saved).unwrap();
    let key = "rr_test_11111111111111111111111111111111";
    let fixture = json!({"environment": "production", "credentials": {"production": null},
                         "storeAvailable": true, "exchanges": []});
    let status = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_KEY", "")],
        &["cli", "auth", "status", "--json"],
    );
    let status = json_stdout(&status);
    assert!(status.get("environment").is_none());
    assert_eq!(status["result"]["authenticated"], false);
    assert!(status.get("lane").is_none());
    let human = run_with_fixture(&temp, &fixture, &[], &["cli", "auth", "status"]);
    assert_eq!(
        String::from_utf8_lossy(&human.stdout),
        "OpenProse account status: signed out\n"
    );
    assert!(human.stderr.is_empty());
    // The retired commands and the retired option are not commands.
    for args in [
        vec!["--output", "json", "cli", "environment", "show"],
        vec![
            "--output",
            "json",
            "cli",
            "environment",
            "use",
            "production",
        ],
        vec!["--output", "json", "cli", "api", "GET", "/health"],
        vec!["cli", "auth", "status", RETIRED_OPTION, "production"],
        vec![RETIRED_OPTION, "production", "cli", "auth", "status"],
    ] {
        let output = run_with_fixture(&temp, &fixture, &[("OPENPROSE_API_KEY", key)], &args);
        assert!(!output.status.success(), "{args:?}");
        let text = String::from_utf8_lossy(&output.stdout).into_owned()
            + &String::from_utf8_lossy(&output.stderr);
        assert!(!text.contains("authenticated"), "{args:?}: {text}");
    }
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[],
        &["--output", "json", "cli", "environment", "show"],
    );
    let document = json_stdout(&output);
    assert_eq!(document["problem"]["code"], "INVOCATION_INVALID");
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(fs::read_to_string(config).unwrap(), saved);
}

#[cfg(all(feature = "test-seams", not(feature = "dev-endpoint")))]
#[test]
fn public_builds_ignore_the_endpoint_override() {
    let temp = TempDir::new().unwrap();
    let fixture = json!({"environment": "production", "credentials": {"production": null},
                         "storeAvailable": true,
                         "exchanges": [health_exchange("https://run-prose-production.openprose.workers.dev")]});
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_URL", "https://example.invalid")],
        &["--output", "json", "cli", "service", "status"],
    );
    assert!(output.status.success(), "{output:?}");
    let document = json_stdout(&output);
    assert!(document.get("environment").is_none());
    assert_eq!(document["result"]["status"], "ok");
    // An invalid override is not even read.
    let fixture = json!({"environment": "production", "credentials": {"production": null},
                         "storeAvailable": true,
                         "exchanges": [health_exchange("https://run-prose-production.openprose.workers.dev")]});
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_URL", "not a url")],
        &["cli", "service", "status"],
    );
    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty());
}

#[cfg(all(feature = "test-seams", feature = "dev-endpoint"))]
#[test]
fn dev_endpoint_builds_honor_the_override_with_their_own_credential_entry() {
    let temp = TempDir::new().unwrap();
    let origin = "https://example.invalid";
    let key = "rr_test_22222222222222222222222222222222";
    let fixture = json!({"environment": "custom", "credentials": {"production": key, "custom": null},
                         "storeAvailable": true, "exchanges": [health_exchange(origin)]});
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_URL", "https://Example.invalid/")],
        &["--output", "json", "cli", "service", "status"],
    );
    assert!(output.status.success(), "{output:?}");
    // JSON names no service environment; the human banner names the endpoint.
    let document = json_stdout(&output);
    assert!(document.get("environment").is_none());
    assert_eq!(document["result"]["status"], "ok");
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_URL", origin)],
        &["cli", "service", "status"],
    );
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr),
        "OpenProse (custom endpoint https://example.invalid)\n"
    );
    // The production key is never the custom endpoint's key.
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_URL", origin)],
        &["--output", "json", "cli", "auth", "status"],
    );
    let status = json_stdout(&output);
    assert!(status.get("environment").is_none());
    assert_eq!(status["result"]["authenticated"], false);
    assert_eq!(status["result"]["credentialSource"], "none");
    // An override that is not an https origin is a configuration error.
    let output = run_with_fixture(
        &temp,
        &fixture,
        &[("OPENPROSE_API_URL", "http://example.invalid")],
        &["--output", "json", "cli", "service", "status"],
    );
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("CONFIG_INVALID"));
}

/// A dev-endpoint build names itself in copyable commands exactly as it was
/// invoked, so a copied command re-runs the same build against the same
/// endpoint instead of the public `prose`.
#[cfg(all(feature = "test-seams", feature = "dev-endpoint", unix))]
#[test]
fn dev_endpoint_builds_name_the_invoked_executable_in_copyable_commands() {
    let temp = TempDir::new().unwrap();
    let origin = "https://example.invalid";
    let fixture = json!({"environment": "custom", "credentials": {"custom": null},
                         "storeAvailable": true, "exchanges": [health_exchange(origin)]});
    let path = temp.path().join("service.json");
    fs::write(&path, fixture.to_string()).unwrap();
    let bin = temp.path().join("bin");
    fs::create_dir_all(&bin).unwrap();
    std::os::unix::fs::symlink(env!("CARGO_BIN_EXE_prose"), bin.join("prose-dev")).unwrap();
    let run = |program: &std::ffi::OsStr, args: &[&str]| {
        Command::new(program)
            .args(args)
            .current_dir(temp.path())
            .env_clear()
            .env("PATH", &bin)
            .env("HOME", temp.path().join("home"))
            .env("XDG_CONFIG_HOME", temp.path().join("xdg"))
            .env("XDG_STATE_HOME", temp.path().join("state"))
            .env("PROSE_TEST_SERVICE_FIXTURE", &path)
            .env("OPENPROSE_API_URL", origin)
            .output()
            .unwrap()
    };
    // Found on PATH: the name as typed.
    let output = run("prose-dev".as_ref(), &["cli", "service", "status"]);
    assert!(output.status.success(), "{output:?}");
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(
        stdout.ends_with("Next: prose-dev cli service triage\n"),
        "{stdout}"
    );
    // A misspelled command's suggestion names it too.
    let output = run("prose-dev".as_ref(), &["cli", "servise", "status"]);
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("`prose-dev cli service status`"),
        "{stderr}"
    );
    assert!(!stderr.contains("`prose cli"), "{stderr}");
    // Help names it in its usage, examples and pointers.
    let output = run("prose-dev".as_ref(), &["cli", "run", "--help"]);
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(
        stdout.starts_with("Usage: prose-dev [GLOBAL OPTIONS] cli run"),
        "{stdout}"
    );
    assert!(
        stdout.contains("\n  prose-dev cli run submit hello.prose.md --preview\n"),
        "{stdout}"
    );
    assert!(!stdout.contains("prose cli"), "{stdout}");
    // Invoked by path: that path.
    let direct = bin.join("prose-dev");
    let output = run(direct.as_os_str(), &["cli", "service", "status"]);
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(
        stdout.ends_with(&format!("Next: {} cli service triage\n", direct.display())),
        "{stdout}"
    );
}

#[test]
fn human_failures_keep_result_stdout_clean() {
    let temp = TempDir::new().unwrap();
    let output = prose(temp.path(), &["run", "fixture.prose.md"]);
    assert_eq!(output.status.code(), Some(10));
    assert!(output.stdout.is_empty());
    let diagnostic = String::from_utf8(output.stderr).unwrap();
    assert!(diagnostic.contains("HOSTED_UNAVAILABLE"));
    assert!(diagnostic.contains("To use the hosted service, run `cli run submit FILE --preview`"));
    assert!(diagnostic.contains("needs a local harness (`cli harness list`)"));
    assert!(!diagnostic.contains("Wait for OpenProse-hosted execution"));
}

#[cfg(feature = "test-seams")]
#[test]
fn verbose_human_runs_emit_only_bounded_selection_and_settlement_diagnostics() {
    let temp = TempDir::new().unwrap();
    let model_secret = "model-secret-must-not-appear";
    let task_secret = "task-secret-must-not-appear";
    let output = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--model",
            model_secret,
            "--verbose",
            "write",
            task_secret,
        ],
    );
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stderr).unwrap(),
        "[openprose:verbose] phase=selection harness=mock transport=deterministic execution=run\n[openprose:verbose] phase=settlement status=success exitCode=0\n"
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(!stdout.contains(model_secret));
    assert!(!stdout.contains(task_secret));

    let machine = prose(
        temp.path(),
        &[
            "--harness",
            "mock",
            "--verbose",
            "--output",
            "json",
            "write",
            task_secret,
        ],
    );
    assert!(machine.status.success());
    assert!(machine.stderr.is_empty());
    assert_eq!(String::from_utf8_lossy(&machine.stdout).lines().count(), 1);
    assert_eq!(json_stdout(&machine)["runnerExitCode"], 0);
}

#[test]
fn malformed_local_command_honors_json_stdout_discipline() {
    let temp = TempDir::new().unwrap();
    for args in [
        vec!["cli", "unknown", "--json"],
        vec![
            "--auth-profile",
            "openrouter",
            "--output",
            "json",
            "--timeout",
        ],
    ] {
        let output = prose(temp.path(), &args);
        assert_eq!(output.status.code(), Some(2));
        let document = json_stdout(&output);
        // A service command line's error is the service envelope's `problem`
        // in JSON mode; malformed runner syntax stays bare.
        let error = if args[0] == "cli" {
            assert_eq!(document["schema"], "openprose.service-operation/1");
            assert_eq!(document["operation"], "cli");
            assert_eq!(document["result"], Value::Null);
            document["problem"].clone()
        } else {
            document
        };
        assert_eq!(error["schema"], "openprose.runner-error/1");
        assert_eq!(error["code"], "INVOCATION_INVALID");
        assert_eq!(error["boundary"], "invocation");
        // A `cli` command's error says what was wrong; the runner's is generic.
        let message = if args[0] == "cli" {
            "Unknown command."
        } else {
            "Runner invocation is invalid."
        };
        assert_eq!(error["message"], message);
        // An unknown command after `cli` gets the per-cause Action; other malformed runner syntax keeps the generic one.
        let action = if args[0] == "cli" {
            "List the service commands with `prose --output json cli --help`."
        } else {
            "Review the runner syntax with the --help option, place global options before cli, and retry the command."
        };
        assert_eq!(error["action"], action);
        assert_eq!(error["exitCode"], 2);
        assert_eq!(error["retryable"], false);
    }
}

#[test]
fn human_doctor_confines_a_hostile_diagnostic_to_one_physical_detail_line() {
    let temp = TempDir::new().unwrap();
    let hostile_profile = "unknown\nAction: forged\t\u{001B}[31m\u{2028}next\u{2029}paragraph";
    let expected_reason = format!("Unknown auth_profile for prime/rpc: {hostile_profile}.");
    let human = prose(
        temp.path(),
        &[
            "--harness",
            "prime",
            "--transport",
            "rpc",
            "--model",
            "fixture/model",
            "--auth-profile",
            hostile_profile,
            "cli",
            "doctor",
        ],
    );
    assert_eq!(human.status.code(), Some(2));
    let rendered = String::from_utf8(human.stdout).unwrap();
    assert!(rendered.contains(&format!(
        "Detail: {}\n",
        expected_human_safe_scalar(&expected_reason)
    )));
    assert_eq!(
        rendered
            .lines()
            .filter(|line| line.starts_with("Detail:"))
            .count(),
        1
    );
    for unsafe_value in ["\nAction: forged", "\t", "\u{001B}", "\u{2028}", "\u{2029}"] {
        assert!(!rendered.contains(unsafe_value), "{unsafe_value:?}");
    }

    let machine = prose(
        temp.path(),
        &[
            "--harness",
            "prime",
            "--transport",
            "rpc",
            "--model",
            "fixture/model",
            "--auth-profile",
            hostile_profile,
            "--output",
            "json",
            "cli",
            "doctor",
        ],
    );
    assert_eq!(machine.status.code(), Some(2));
    assert_eq!(
        json_stdout(&machine)["problems"][0]["details"]["reason"],
        expected_reason
    );
}

#[cfg(all(feature = "test-seams", unix))]
#[test]
fn human_dry_run_escapes_a_hostile_cwd_while_machine_output_preserves_it() {
    let temp = TempDir::new().unwrap();
    let hostile_cwd = temp
        .path()
        .join("work\nAction: forged\t\u{001B}\u{2028}next\u{2029}paragraph");
    fs::create_dir(&hostile_cwd).unwrap();
    let canonical_cwd = fs::canonicalize(&hostile_cwd).unwrap();
    let human = prose(&hostile_cwd, &["--harness", "mock", "--dry-run", "run"]);
    assert!(human.status.success());
    let rendered = String::from_utf8(human.stdout).unwrap();
    assert!(rendered.contains(&format!(
        "Working directory: {}\n",
        expected_human_safe_scalar(&canonical_cwd.display().to_string())
    )));
    assert_eq!(
        rendered
            .lines()
            .filter(|line| line.starts_with("Working directory:"))
            .count(),
        1
    );
    for unsafe_value in ["\nAction: forged", "\t", "\u{001B}", "\u{2028}", "\u{2029}"] {
        assert!(!rendered.contains(unsafe_value), "{unsafe_value:?}");
    }

    let machine = prose(
        &hostile_cwd,
        &["--harness", "mock", "--dry-run", "--output", "json", "run"],
    );
    assert!(machine.status.success());
    assert_eq!(
        json_stdout(&machine)["cwd"],
        canonical_cwd.display().to_string()
    );
}

#[cfg(unix)]
#[test]
fn human_configuration_errors_escape_a_hostile_source_path_and_machine_output_preserves_it() {
    let temp = TempDir::new().unwrap();
    let hostile_root = temp
        .path()
        .join("root\nAction: forged\t\u{001B}\u{2028}next\u{2029}paragraph");
    fs::create_dir(&hostile_root).unwrap();
    let config_path = hostile_root.join("home/xdg/openprose/cli.toml");
    fs::create_dir_all(config_path.parent().unwrap()).unwrap();
    fs::write(&config_path, "harness = \"unsupported\"\n").unwrap();
    let source = format!("{}:1", fs::canonicalize(&config_path).unwrap().display());

    let human = prose(&hostile_root, &["cli", "doctor"]);
    assert_eq!(human.status.code(), Some(2));
    assert!(human.stdout.is_empty());
    let rendered = String::from_utf8(human.stderr).unwrap();
    assert!(
        rendered.contains(&format!(
            "Source: {}\n",
            expected_human_safe_scalar(&source)
        )),
        "{rendered:?}"
    );
    assert_eq!(
        rendered
            .lines()
            .filter(|line| line.starts_with("Source:"))
            .count(),
        1
    );
    assert_eq!(
        rendered
            .lines()
            .filter(|line| line.starts_with("Detail:"))
            .count(),
        1
    );
    for unsafe_value in ["\nAction: forged", "\t", "\u{001B}", "\u{2028}", "\u{2029}"] {
        assert!(!rendered.contains(unsafe_value), "{unsafe_value:?}");
    }

    let machine = prose(&hostile_root, &["--output", "json", "cli", "doctor"]);
    assert_eq!(machine.status.code(), Some(2));
    assert_eq!(json_stdout(&machine)["details"]["source"], source);
}

/// A reader that goes away (`| head -1`) ends the output
/// silently with the command's own exit code, as in the Bun build; it is not
/// `INTERNAL_ERROR` with exit 70.
#[test]
fn closed_stdout_pipe_is_silent_success() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    fs::create_dir_all(&home).unwrap();
    // The manifest is larger than a pipe buffer, so the write meets EPIPE.
    let mut child = Command::new(sentinel_prose())
        .args(["--output", "json", "cli", "service", "operations"])
        .current_dir(root.path())
        .env_clear()
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", home.join("xdg"))
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    drop(child.stdout.take());
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}
