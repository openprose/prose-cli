//! Explicit, bounded local coordinator transport. No language or lifecycle semantics.
use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug)]
struct Failure(&'static str, u8);
const INVOCATION: Failure = Failure("WEAVE_HOST_INVOCATION_INVALID", 2);
const BINDING: Failure = Failure("WEAVE_HOST_BINDING_INVALID", 2);
const IO: Failure = Failure("WEAVE_HOST_IO_FAILED", 125);
const TIMEOUT: Failure = Failure("WEAVE_HOST_TIMEOUT", 124);
const OUTPUT: Failure = Failure("WEAVE_HOST_OUTPUT_LIMIT", 125);
const START: Failure = Failure("WEAVE_HOST_START_FAILED", 126);

fn text(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 4096
        && !s.contains('\0')
        && s.chars().any(|c| !c.is_whitespace() && c != '\u{feff}')
}
fn absolute(s: &str) -> bool {
    text(s) && Path::new(s).is_absolute()
}
fn positive(s: &str, max: u64) -> bool {
    !s.starts_with('0')
        && !s.is_empty()
        && s.bytes().all(|b| b.is_ascii_digit())
        && s.parse::<u64>().is_ok_and(|v| v <= max)
}
fn grammar(args: &[String]) -> Result<(&str, &[String]), Failure> {
    let [flag, binding, operation, config, rest @ ..] = args else {
        return Err(INVOCATION);
    };
    if flag != "--host-binding" || !absolute(binding) || !absolute(config) {
        return Err(INVOCATION);
    }
    let valid = match operation.as_str() {
        "check" | "status" | "step" => rest.is_empty(),
        "serve" => {
            matches!(rest, [p,ms,m,n] if p=="--poll-ms" && m=="--max-steps" && positive(ms,3_600_000) && positive(n,1_000_000))
        }
        "settle" => {
            matches!(rest,[b,bv,a,av,o,ov,r,rv] if b=="--binding" && a=="--attempt" && o=="--outcome" && r=="--receipt" && text(bv) && text(av) && text(rv) && matches!(ov.as_str(),"completed"|"not-applied"))
        }
        _ => false,
    };
    if !valid {
        return Err(INVOCATION);
    }
    Ok((binding, &args[2..]))
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Binding {
    schema: String,
    executable: String,
    sha256: String,
    environment_keys: Vec<String>,
    timeout_ms: u64,
    max_output_bytes: u64,
}
/// serde rejects duplicate typed fields and invalid strings; this scan additionally
/// rejects otherwise numerically integral exponent/fraction tokens and negative zero.
fn canonical_numbers(bytes: &[u8]) -> bool {
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'"' {
            i += 1;
            while i < bytes.len() {
                if bytes[i] == b'\\' {
                    i += 2;
                } else if bytes[i] == b'"' {
                    i += 1;
                    break;
                } else {
                    i += 1;
                }
            }
        } else if bytes[i] == b'-' || bytes[i].is_ascii_digit() {
            let start = i;
            while i < bytes.len()
                && !matches!(bytes[i], b',' | b'}' | b']' | b' ' | b'\n' | b'\r' | b'\t')
            {
                i += 1;
            }
            let value = &bytes[start..i];
            if value.is_empty()
                || !value.iter().all(u8::is_ascii_digit)
                || (value.len() > 1 && value[0] == b'0')
            {
                return false;
            }
        } else {
            i += 1;
        }
    }
    true
}
fn decode(bytes: &[u8]) -> Result<Binding, Failure> {
    if !canonical_numbers(bytes) {
        return Err(BINDING);
    }
    let b: Binding = serde_json::from_slice(bytes).map_err(|_| BINDING)?;
    if b.schema != "openprose.weave-host-binding/1"
        || !absolute(&b.executable)
        || b.sha256.len() != 64
        || !b
            .sha256
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        || !(1..=86_400_000).contains(&b.timeout_ms)
        || !(1..=16_777_216).contains(&b.max_output_bytes)
        || b.environment_keys.len() > 128
    {
        return Err(BINDING);
    }
    let mut keys = std::collections::HashSet::new();
    for key in &b.environment_keys {
        if key.is_empty()
            || key.len() > 128
            || !keys.insert(key)
            || !key
                .bytes()
                .enumerate()
                .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
        {
            return Err(BINDING);
        }
    }
    Ok(b)
}
/// `prose cli weave --help`, byte for byte the same in both ports.
const HELP: &[u8] = include_bytes!("../../../../shared/fixtures/weave-help.txt");

pub(super) fn run(globals: bool, args: &[String]) -> u8 {
    let result = (|| {
        if globals {
            return Err(INVOCATION);
        }
        if args == ["--help"] {
            use std::io::Write;
            std::io::stdout().write_all(HELP).map_err(|_| IO)?;
            return Ok(0);
        }
        let (path, child_args) = grammar(args)?;
        #[cfg(unix)]
        {
            unix::execute(path, child_args)
        }
        #[cfg(not(unix))]
        {
            let _ = (path, child_args);
            Err(Failure("WEAVE_HOST_UNSUPPORTED_PLATFORM", 2))
        }
    })();
    match result {
        Ok(code) => code,
        Err(f) => {
            #[cfg(unix)]
            unix::diagnostic(f.0);
            #[cfg(not(unix))]
            {
                use std::io::Write;
                let _ = writeln!(std::io::stderr(), "{}", f.0);
            }
            f.1
        }
    }
}

#[cfg(unix)]
mod unix {
    use super::{BINDING, Binding, Failure, IO, OUTPUT, Path, PathBuf, START, TIMEOUT, decode};
    use rustix::fd::AsFd;
    use rustix::fs::{Mode, OFlags, fcntl_getfl, fcntl_setfl};
    use sha2::{Digest, Sha256};
    use std::{
        fs::File,
        io::Read,
        os::unix::process::{CommandExt, ExitStatusExt},
        process::{Child, Command, Stdio},
        sync::{
            Arc,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        },
        time::{Duration, Instant},
    };
    struct Signals {
        ids: Vec<signal_hook::SigId>,
        interrupt: Arc<AtomicBool>,
        terminate: Arc<AtomicBool>,
        observed: AtomicUsize,
    }
    impl Signals {
        fn new() -> Result<Self, Failure> {
            let mut s = Self {
                ids: vec![],
                interrupt: Arc::new(AtomicBool::new(false)),
                terminate: Arc::new(AtomicBool::new(false)),
                observed: AtomicUsize::new(0),
            };
            for (signal, flag) in [
                (signal_hook::consts::SIGINT, &s.interrupt),
                (signal_hook::consts::SIGTERM, &s.terminate),
            ] {
                s.ids
                    .push(signal_hook::flag::register(signal, Arc::clone(flag)).map_err(|_| IO)?);
            }
            Ok(s)
        }
        fn failure(&self) -> Option<Failure> {
            // Signal arrival order is not recoverable from coalesced safe flags.
            // INT wins if both are pending at first observation; latch forever.
            let candidate = if self.interrupt.load(Ordering::Relaxed) {
                130
            } else if self.terminate.load(Ordering::Relaxed) {
                143
            } else {
                0
            };
            if candidate != 0 {
                let _ = self.observed.compare_exchange(
                    0,
                    candidate,
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                );
            }
            let code = self.observed.load(Ordering::Relaxed);
            (code != 0).then_some(Failure("WEAVE_HOST_CANCELLED", code as u8))
        }
    }
    impl Drop for Signals {
        fn drop(&mut self) {
            for id in &self.ids {
                signal_hook::low_level::unregister(*id);
            }
        }
    }
    fn file(path: &Path) -> Result<File, Failure> {
        let fd = rustix::fs::open(
            path,
            OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| BINDING)?;
        let f = File::from(fd);
        if !f.metadata().map_err(|_| BINDING)?.is_file() {
            return Err(BINDING);
        }
        Ok(f)
    }
    fn load(path: &str) -> Result<(Binding, PathBuf, PathBuf), Failure> {
        let path = std::fs::canonicalize(path).map_err(|_| BINDING)?;
        let mut bytes = vec![];
        file(&path)?
            .take(65_537)
            .read_to_end(&mut bytes)
            .map_err(|_| BINDING)?;
        if bytes.len() > 65_536 {
            return Err(BINDING);
        }
        let b = decode(&bytes)?;
        let executable = std::fs::canonicalize(&b.executable).map_err(|_| BINDING)?;
        rustix::fs::access(&executable, rustix::fs::Access::EXEC_OK).map_err(|_| BINDING)?;
        let mut f = file(&executable)?;
        if f.metadata().map_err(|_| BINDING)?.len() > 536_870_912 {
            return Err(BINDING);
        }
        let mut total = 0;
        let mut sha = Sha256::new();
        let mut chunk = [0u8; 65_536];
        loop {
            let n = f.read(&mut chunk).map_err(|_| BINDING)?;
            if n == 0 {
                break;
            }
            total += n;
            if total > 536_870_912 {
                return Err(BINDING);
            }
            sha.update(&chunk[..n]);
        }
        if format!("{:x}", sha.finalize()) != b.sha256 {
            return Err(BINDING);
        }
        Ok((b, executable, path.parent().ok_or(BINDING)?.to_owned()))
    }
    fn nonblock(fd: &impl AsFd) -> Result<(), Failure> {
        let flags = fcntl_getfl(fd).map_err(|_| IO)?;
        fcntl_setfl(fd, flags | OFlags::NONBLOCK).map_err(|_| IO)
    }
    struct OutputFlags {
        out: std::io::Stdout,
        err: std::io::Stderr,
        out_flags: OFlags,
        err_flags: OFlags,
    }
    impl OutputFlags {
        fn new() -> Result<Self, Failure> {
            let out = std::io::stdout();
            let err = std::io::stderr();
            let out_flags = fcntl_getfl(&out).map_err(|_| IO)?;
            let err_flags = fcntl_getfl(&err).map_err(|_| IO)?;
            let guard = Self {
                out,
                err,
                out_flags,
                err_flags,
            };
            nonblock(&guard.out)?;
            nonblock(&guard.err)?;
            Ok(guard)
        }
    }
    impl Drop for OutputFlags {
        fn drop(&mut self) {
            let _ = fcntl_setfl(&self.out, self.out_flags);
            let _ = fcntl_setfl(&self.err, self.err_flags);
        }
    }
    fn check(signals: &Signals, deadline: Instant) -> Result<(), Failure> {
        if let Some(f) = signals.failure() {
            return Err(f);
        }
        if Instant::now() >= deadline {
            return Err(TIMEOUT);
        }
        Ok(())
    }
    fn forward(
        fd: &impl AsFd,
        mut bytes: &[u8],
        signals: &Signals,
        deadline: Instant,
    ) -> Result<(), Failure> {
        while !bytes.is_empty() {
            check(signals, deadline)?;
            match rustix::io::write(fd, bytes) {
                Ok(0) => return Err(IO),
                Ok(n) => bytes = &bytes[n..],
                Err(rustix::io::Errno::INTR) => {}
                Err(rustix::io::Errno::AGAIN) => std::thread::sleep(Duration::from_millis(2)),
                Err(_) => return Err(IO),
            }
        }
        Ok(())
    }
    #[cfg(test)]
    pub(super) fn test_signal_latch() {
        let flags = Signals {
            ids: vec![],
            interrupt: Arc::new(AtomicBool::new(false)),
            terminate: Arc::new(AtomicBool::new(false)),
            observed: AtomicUsize::new(0),
        };
        flags.terminate.store(true, Ordering::Relaxed);
        assert_eq!(flags.failure().unwrap().1, 143);
        flags.interrupt.store(true, Ordering::Relaxed);
        assert_eq!(flags.failure().unwrap().1, 143);
        flags.observed.store(0, Ordering::Relaxed);
        assert_eq!(flags.failure().unwrap().1, 130);
    }
    pub(super) fn diagnostic(code: &str) {
        let err = std::io::stderr();
        let Ok(flags) = fcntl_getfl(&err) else {
            return;
        };
        if fcntl_setfl(&err, flags | OFlags::NONBLOCK).is_err() {
            return;
        }
        let message = format!("{code}\n");
        let mut bytes = message.as_bytes();
        let until = Instant::now() + Duration::from_millis(100);
        while !bytes.is_empty() && Instant::now() < until {
            match rustix::io::write(&err, bytes) {
                Ok(0) => break,
                Ok(n) => bytes = &bytes[n..],
                Err(rustix::io::Errno::INTR) => {}
                Err(rustix::io::Errno::AGAIN) => std::thread::sleep(Duration::from_millis(2)),
                Err(_) => break,
            }
        }
        let _ = fcntl_setfl(&err, flags);
    }
    fn stop(child: &mut Child) {
        if !matches!(child.try_wait(), Ok(None)) {
            return;
        }
        if let Some(pid) = rustix::process::Pid::from_raw(child.id() as i32) {
            let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::TERM);
        }
        let until = Instant::now() + Duration::from_millis(1000);
        while Instant::now() < until {
            if !matches!(child.try_wait(), Ok(None)) {
                return;
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        if matches!(child.try_wait(), Ok(None)) {
            if let Some(pid) = rustix::process::Pid::from_raw(child.id() as i32) {
                let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
            }
            let _ = child.wait();
        }
    }
    pub(super) fn execute(path: &str, args: &[String]) -> Result<u8, Failure> {
        let signals = Signals::new()?;
        let (b, executable, cwd) = load(path)?;
        if let Some(f) = signals.failure() {
            return Err(f);
        }
        let outputs = OutputFlags::new()?;
        let mut command = Command::new(executable);
        command
            .args(args)
            .current_dir(cwd)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        for key in &b.environment_keys {
            if let Some(v) = std::env::var_os(key) {
                command.env(key, v);
            }
        }
        let deadline = Instant::now() + Duration::from_millis(b.timeout_ms);
        let mut child = command.spawn().map_err(|_| START)?;
        let result = (|| {
            let mut out = child.stdout.take().ok_or(IO)?;
            let mut err = child.stderr.take().ok_or(IO)?;
            nonblock(&out)?;
            nonblock(&err)?;
            let mut eof = [false, false];
            let mut status = None;
            let mut remaining = b.max_output_bytes as usize;
            let mut chunk = [0u8; 8192];
            loop {
                if status.is_none() {
                    status = child.try_wait().map_err(|_| IO)?;
                }
                if let Some(s) = status {
                    if eof == [true, true] {
                        return Ok(s
                            .code()
                            .map_or_else(|| (128 + s.signal().unwrap_or(1)) as u8, |n| n as u8));
                    }
                }
                check(&signals, deadline)?;
                let mut progress = false;
                for i in 0..2 {
                    if eof[i] {
                        continue;
                    }
                    let read = if i == 0 {
                        out.read(&mut chunk)
                    } else {
                        err.read(&mut chunk)
                    };
                    match read {
                        Ok(0) => {
                            eof[i] = true;
                            progress = true;
                        }
                        Ok(n) => {
                            progress = true;
                            let allowed = n.min(remaining);
                            let overflow = n > allowed;
                            // Overflow is observed before forwarding. A blocked/closed
                            // consumer or later signal cannot replace this first failure.
                            let flush_deadline = if overflow {
                                deadline.min(Instant::now() + Duration::from_millis(100))
                            } else {
                                deadline
                            };
                            let forwarded = if i == 0 {
                                forward(&outputs.out, &chunk[..allowed], &signals, flush_deadline)
                            } else {
                                forward(&outputs.err, &chunk[..allowed], &signals, flush_deadline)
                            };
                            remaining -= allowed;
                            if overflow {
                                return Err(OUTPUT);
                            }
                            forwarded?;
                        }
                        Err(e)
                            if matches!(
                                e.kind(),
                                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                            ) => {}
                        Err(_) => return Err(IO),
                    }
                }
                if !progress {
                    std::thread::sleep(Duration::from_millis(2));
                }
            }
        })();
        if result.is_err() {
            stop(&mut child);
        }
        result
    }
}

#[cfg(all(test, unix))]
mod cancellation_tests {
    #[test]
    fn simultaneous_flags_choose_int_and_observed_cancel_is_sticky() {
        super::unix::test_signal_latch();
    }
}
