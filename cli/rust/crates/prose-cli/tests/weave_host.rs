#![cfg(unix)]
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::{Command, Output},
    time::{Duration, Instant},
};
struct Fixture {
    _root: tempfile::TempDir,
    root: PathBuf,
    binding: PathBuf,
    host: PathBuf,
}
impl Fixture {
    fn new(body: &str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let host = root.join("host");
        fs::write(&host, format!("#!/usr/bin/python3\n{body}\n")).unwrap();
        fs::set_permissions(&host, fs::Permissions::from_mode(0o700)).unwrap();
        let binding = root.join("binding.json");
        let f = Self {
            _root: temp,
            root,
            binding,
            host,
        };
        f.save(f.value());
        f
    }
    fn value(&self) -> Value {
        json!({"schema":"openprose.weave-host-binding/1","executable":self.host,"sha256":format!("{:x}",Sha256::digest(fs::read(&self.host).unwrap())),"environmentKeys":[],"timeoutMs":2000,"maxOutputBytes":65536})
    }
    fn save(&self, v: Value) {
        fs::write(&self.binding, serde_json::to_vec(&v).unwrap()).unwrap();
    }
    fn command(&self) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_prose"));
        c.env_clear()
            .args(["cli", "weave", "--host-binding"])
            .arg(&self.binding)
            .arg("status")
            .arg(self.root.join("config"));
        c
    }
    fn run(&self) -> Output {
        self.command().output().unwrap()
    }
}
#[test]
fn preserves_raw_streams_and_nonzero_exit() {
    let f = Fixture::new(
        "import os,sys\nos.write(1,b'\\xff\\x00\\n')\nos.write(2,b'\\xfe')\nsys.exit(42)",
    );
    let r = f.run();
    assert_eq!(r.status.code(), Some(42));
    assert_eq!(r.stdout, b"\xff\0\n");
    assert_eq!(r.stderr, b"\xfe");
}
#[test]
fn forwards_exact_argv_closed_stdin_cwd_and_only_present_selected_environment() {
    let f = Fixture::new(
        "import os,sys,json\nassert sys.stdin.buffer.read()==b''\nassert os.environ.get('PRESENT')=='chosen'\nassert os.environ.get('__proto__')=='literal'\nassert 'ABSENT' not in os.environ and 'UNSELECTED' not in os.environ\nprint(json.dumps([sys.argv[1:],os.getcwd()]))",
    );
    let mut b = f.value();
    b["environmentKeys"] = json!(["PRESENT", "ABSENT", "__proto__"]);
    f.save(b);
    let r = f
        .command()
        .env("PRESENT", "chosen")
        .env("__proto__", "literal")
        .env("UNSELECTED", "secret")
        .output()
        .unwrap();
    assert!(r.status.success(), "{:?}", r.stderr);
    let v: Value = serde_json::from_slice(&r.stdout).unwrap();
    assert_eq!(v, json!([["status", f.root.join("config")], f.root]));
}
#[test]
fn rejects_ambiguous_binding_before_effect() {
    let f = Fixture::new("from pathlib import Path\nPath('effect').write_text('effect')");
    let raw = f.value().to_string();
    for broken in [
        raw.replace("\"timeoutMs\":2000", "\"timeoutMs\":2e3"),
        raw.replace("\"timeoutMs\":2000", "\"timeoutMs\":2000.0"),
        raw.replace("\"timeoutMs\":2000", "\"timeoutMs\":-0"),
        format!("{},\"schema\":\"extra\"}}", &raw[..raw.len() - 1]),
        format!("{},\"\\u0073chema\":\"extra\"}}", &raw[..raw.len() - 1]),
        raw.replace(
            "\"environmentKeys\":[]",
            "\"environmentKeys\":[\"\\ud800\"]",
        ),
        format!("\u{feff}{raw}"),
    ] {
        fs::write(&f.binding, broken).unwrap();
        let r = f.run();
        assert_eq!(r.status.code(), Some(2));
        assert_eq!(r.stderr, b"WEAVE_HOST_BINDING_INVALID\n");
        assert!(!f.root.join("effect").exists());
    }
}
#[test]
fn mismatched_host_invalid_grammar_and_global_never_start() {
    let f = Fixture::new("from pathlib import Path\nPath('effect').write_text('effect')");
    let mut b = f.value();
    b["sha256"] = json!("0".repeat(64));
    f.save(b);
    assert_eq!(f.run().status.code(), Some(2));
    let r = Command::new(env!("CARGO_BIN_EXE_prose"))
        .env_clear()
        .args(["--dry-run", "cli", "weave", "--host-binding"])
        .arg(&f.binding)
        .arg("step")
        .arg(f.root.join("config"))
        .output()
        .unwrap();
    assert_eq!(r.stderr, b"WEAVE_HOST_INVOCATION_INVALID\n");
    assert!(!f.root.join("effect").exists());
}
#[test]
fn output_budget_and_timeout_are_bounded() {
    let f = Fixture::new("import os,time\nos.write(1,b'12345')\ntime.sleep(5)");
    let mut b = f.value();
    b["maxOutputBytes"] = json!(4);
    f.save(b);
    let now = Instant::now();
    let r = f.run();
    assert_eq!(r.status.code(), Some(125));
    assert_eq!(r.stdout, b"1234");
    assert_eq!(r.stderr, b"WEAVE_HOST_OUTPUT_LIMIT\n");
    assert!(now.elapsed() < Duration::from_secs(3));
    let f = Fixture::new(
        "import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(5)",
    );
    let mut b = f.value();
    b["timeoutMs"] = json!(100);
    f.save(b);
    let now = Instant::now();
    let r = f.run();
    assert_eq!(r.status.code(), Some(124));
    assert_eq!(r.stderr, b"WEAVE_HOST_TIMEOUT\n");
    assert!(now.elapsed() < Duration::from_secs(3));
}
#[test]
fn explicit_signal_preserves_pending_and_returns_signal_code() {
    for (signal, exit) in [
        (rustix::process::Signal::INT, 130),
        (rustix::process::Signal::TERM, 143),
    ] {
        let f = Fixture::new(
            "from pathlib import Path\nimport time\nPath('pending').write_text('attempt-1')\ntime.sleep(5)",
        );
        let mut child = f
            .command()
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        let until = Instant::now() + Duration::from_secs(3);
        while !f.root.join("pending").exists() {
            assert!(Instant::now() < until);
            assert!(child.try_wait().unwrap().is_none());
            std::thread::sleep(Duration::from_millis(5));
        }
        rustix::process::kill_process(
            rustix::process::Pid::from_raw(child.id() as i32).unwrap(),
            signal,
        )
        .unwrap();
        let r = child.wait_with_output().unwrap();
        assert_eq!(r.status.code(), Some(exit));
        assert_eq!(r.stderr, b"WEAVE_HOST_CANCELLED\n");
        assert_eq!(
            fs::read_to_string(f.root.join("pending")).unwrap(),
            "attempt-1"
        );
    }
}

#[test]
fn observed_overflow_precedes_blocked_consumer_timeout() {
    use std::io::Write;
    use std::os::{fd::OwnedFd, unix::net::UnixStream};
    let f = Fixture::new("import os\nos.write(1,b'AB')");
    let mut b = f.value();
    b["maxOutputBytes"] = json!(1);
    b["timeoutMs"] = json!(2000);
    f.save(b);
    let (_reader, mut writer) = UnixStream::pair().unwrap();
    writer.set_nonblocking(true).unwrap();
    loop {
        match writer.write(&[b'x'; 8192]) {
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(e) => panic!("{e}"),
        }
    }
    writer.set_nonblocking(false).unwrap();
    let r = f
        .command()
        .stdout(std::process::Stdio::from(OwnedFd::from(writer)))
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap()
        .wait_with_output()
        .unwrap();
    assert_eq!(r.status.code(), Some(125));
    assert_eq!(r.stderr, b"WEAVE_HOST_OUTPUT_LIMIT\n");
}

#[test]
fn second_signal_cannot_replace_observed_cancellation_during_grace() {
    for (first, second, code) in [
        (
            rustix::process::Signal::INT,
            rustix::process::Signal::TERM,
            130,
        ),
        (
            rustix::process::Signal::TERM,
            rustix::process::Signal::INT,
            143,
        ),
    ] {
        let f = Fixture::new(
            "from pathlib import Path\nimport signal,time\nsignal.signal(signal.SIGTERM,lambda *_:Path('grace').write_text('observed'))\nPath('ready').write_text('ready')\ntime.sleep(5)",
        );
        let mut child = f
            .command()
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        let until = Instant::now() + Duration::from_secs(3);
        while !f.root.join("ready").exists() {
            assert!(Instant::now() < until);
            assert!(child.try_wait().unwrap().is_none());
            std::thread::sleep(Duration::from_millis(2));
        }
        let pid = rustix::process::Pid::from_raw(child.id() as i32).unwrap();
        rustix::process::kill_process(pid, first).unwrap();
        // The owned host received the bridge's TERM: the first cancellation has
        // been observed and the bridge is demonstrably in its grace interval.
        while !f.root.join("grace").exists() {
            assert!(Instant::now() < until);
            assert!(child.try_wait().unwrap().is_none());
            std::thread::sleep(Duration::from_millis(2));
        }
        rustix::process::kill_process(pid, second).unwrap();
        let r = child.wait_with_output().unwrap();
        assert_eq!(r.status.code(), Some(code));
        assert_eq!(r.stderr, b"WEAVE_HOST_CANCELLED\n");
    }
}
