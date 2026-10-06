#![cfg(all(feature = "test-seams", unix))]

use rustix::process::{Pid, Signal, kill_process, test_kill_process};
use serde_json::Value;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

struct Attempt {
    child: Option<Child>,
    pid_file: PathBuf,
}

impl Attempt {
    fn native_pid(&self) -> Option<Pid> {
        fs::read_to_string(&self.pid_file)
            .ok()
            .and_then(|text| text.parse::<i32>().ok())
            .filter(|raw| *raw > 1)
            .and_then(Pid::from_raw)
    }

    fn output(&mut self) -> Output {
        let deadline = Instant::now() + Duration::from_secs(8);
        while self.child.as_mut().unwrap().try_wait().unwrap().is_none() {
            assert!(
                Instant::now() < deadline,
                "compiled CLI did not settle within eight seconds"
            );
            thread::sleep(Duration::from_millis(10));
        }
        self.child.take().unwrap().wait_with_output().unwrap()
    }

    fn assert_native_absent(&self) {
        let pid = self
            .native_pid()
            .expect("the synthetic native child published its PID");
        assert_eq!(
            test_kill_process(pid),
            Err(rustix::io::Errno::SRCH),
            "native child remains alive or its absence is unestablished"
        );
        fs::remove_file(&self.pid_file).unwrap();
    }
}

impl Drop for Attempt {
    fn drop(&mut self) {
        let pid = self.native_pid();
        if let Some(pid) = pid {
            let _ = kill_process(pid, Signal::KILL);
        }
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        if let Some(pid) = pid {
            let deadline = Instant::now() + Duration::from_secs(1);
            while test_kill_process(pid).is_ok() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

fn required_bun() -> PathBuf {
    std::env::split_paths(&std::env::var_os("PATH").expect("Bun admission requires PATH"))
        .map(|directory| directory.join("bun"))
        .find(|candidate| {
            candidate.is_file()
                && fs::metadata(candidate).unwrap().permissions().mode() & 0o111 != 0
        })
        .expect("Bun is a required admission tool; install the pinned version before testing")
        .canonicalize()
        .unwrap()
}

fn invoke(root: &Path, arguments: &[&str], pid_file: &Path) -> Attempt {
    let child = Command::new(env!("CARGO_BIN_EXE_prose"))
        .args(arguments)
        .current_dir(root)
        .env_clear()
        .env("PATH", root)
        .env("HOME", root)
        .env("XDG_CONFIG_HOME", root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    Attempt {
        child: Some(child),
        pid_file: pid_file.to_owned(),
    }
}

fn write_harness(root: &Path, bun: &Path, pid_file: &Path, cell: &Value) {
    let alias = root.join("bun");
    std::os::unix::fs::symlink(bun, &alias).unwrap();
    let source = format!(
        r"#!{}
const fs = require('node:fs');
if (process.argv.includes('--version')) console.log('codex-cli 0.149.0-alpha.4.1');
else if (process.argv.slice(2).join(' ') === 'login status') console.log('Logged in using ChatGPT');
else {{
  fs.writeFileSync({}, String(process.pid));
  console.log(JSON.stringify({{type:'thread.started',thread_id:'fixture'}}));
  console.log(JSON.stringify({{type:'turn.started'}}));
  const record = {{type:'item.completed',item:{{type:'command_execution',id:'tool1',command:'fixture',aggregated_output:'',exit_code:0,status:'completed'}}}};
  record.item.aggregated_output = 'x'.repeat({} - Buffer.byteLength(JSON.stringify(record)));
  console.log(JSON.stringify(record));
  if ({}) console.log(JSON.stringify({{type:'turn.completed',usage:{{input_tokens:1,output_tokens:1,cached_input_tokens:0}}}}));
  else setInterval(() => {{}}, 1000);
}}
",
        alias.display(),
        serde_json::to_string(&pid_file).unwrap(),
        cell["recordBytes"],
        cell["accepted"]
    );
    let executable = root.join("codex");
    fs::write(&executable, source).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
}

#[test]
fn compiled_native_record_boundary_and_recovery_match_shared_controls() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../shared/fixtures/adapters/native-output.v1.json"
    ))
    .unwrap();
    let limits = &fixture["recordLimits"];
    let bun = required_bun();
    for cell in limits["cases"].as_array().unwrap() {
        let root = tempfile::tempdir().unwrap();
        let pid_file = root.path().join("native.pid");
        write_harness(root.path(), &bun, &pid_file, cell);
        let aggregate = cell["aggregateBytes"].to_string();
        let arguments = [
            "--harness",
            "codex",
            "--auth-profile",
            "cached-chatgpt-login",
            "--output-contract",
            "native",
            "--native-output-bytes",
            aggregate.as_str(),
            "--timeout",
            "5s",
            "--output",
            "json",
            "--",
            "execute",
            "fixture.md",
        ];
        let mut attempt = invoke(root.path(), &arguments, &pid_file);
        let output = attempt.output();
        assert!(
            output.stderr.is_empty(),
            "{}: unexpected stderr",
            cell["name"]
        );
        let result: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(
            result["nativeOutputLimits"]["maxRecordBytes"],
            limits["recordLimitBytes"]
        );
        assert_eq!(
            result["nativeOutputLimits"]["maxAggregateStdoutBytes"],
            cell["aggregateBytes"]
        );
        if cell["accepted"] == true {
            assert!(output.status.success());
            assert_eq!(result["terminal"]["transportCompleted"], true);
            assert_eq!(result["semantic"]["status"], "not-applicable");
        } else {
            assert_eq!(output.status.code(), Some(22));
            for (key, expected) in limits["error"].as_object().unwrap() {
                assert_eq!(&result["error"][key], expected, "{}: {key}", cell["name"]);
            }
            let diagnostic = &result["error"]["details"]["transportDiagnostic"];
            assert_eq!(diagnostic["reason"], "record-byte-limit");
            assert_eq!(diagnostic["limitBytes"], limits["recordLimitBytes"]);
            assert!(
                diagnostic["observedBytes"].as_u64().unwrap()
                    > limits["recordLimitBytes"].as_u64().unwrap()
            );
            assert_eq!(result["error"]["details"]["terminalEventObserved"], false);
            assert_eq!(result["terminal"]["transportCompleted"], false);
            assert_eq!(result["terminal"]["terminalEventObserved"], false);
            assert_eq!(result["semantic"]["status"], "unknown");
        }
        attempt.assert_native_absent();
        let arguments = [
            "--harness",
            "codex",
            "--auth-profile",
            "cached-chatgpt-login",
            "--output-contract",
            "native",
            "--native-output-bytes",
            aggregate.as_str(),
            "cli",
            "doctor",
            "--json",
        ];
        let mut doctor = invoke(root.path(), &arguments, &pid_file);
        let output = doctor.output();
        assert!(output.status.success());
        assert!(output.stderr.is_empty());
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["nativeOutputLimits"], result["nativeOutputLimits"]);
        assert!(
            !pid_file.exists(),
            "doctor must not launch a native execution"
        );
    }
}
