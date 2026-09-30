//! Linux operating-system credential store through libsecret's `secret-tool`
//! (see `docs/service/credentials.md`).
//!
//! Items are interchangeable with the ones `Bun.secrets` writes, so the Rust
//! and Bun products share one login. The attributes were discovered
//! empirically from Bun 1.3.5 (`secret-tool search --all`):
//!
//! | attribute | value |
//! | --- | --- |
//! | `xdg:schema` | `com.oven-sh.bun.Secret` (Bun only matches items carrying it) |
//! | `service` | `org.openprose.cli.production` (a `dev-endpoint` build's custom origin: `org.openprose.cli.custom-<origin digest>`) |
//! | `account` | `api-key` |
//!
//! and the label is `<service>/api-key`.
//!
//! The tool is resolved from fixed system locations before `PATH` (and a
//! `PATH` candidate must pass ownership/writability checks), runs in its own
//! process group with a fixed argument vector, no shell, the secret only on
//! standard input, a 10 s bound, and an environment cleared except for
//! `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR`. Any failure is
//! `CREDENTIAL_STORE_UNAVAILABLE`; there is no plaintext fallback.
//!
//! The store only transports: `get` returns the stored value unchanged (no
//! newline stripping, no key-format check), exactly like `Bun.secrets.get`.
//! Every caller validates the value itself, so both ports classify a
//! malformed stored value identically.
/// `get`, `set` or `delete` the service's API key in the Secret Service.
///
/// `get` and `delete` report a missing item as `Ok(None)`. Other platforms
/// have no Secret Service and always report `CREDENTIAL_STORE_UNAVAILABLE`.
#[cfg_attr(target_os = "macos", allow(dead_code))]
pub(crate) fn linux_store(
    operation: &str,
    token: Option<&str>,
    cancellation: &crate::CancellationToken,
    environment: &crate::service::Environment,
) -> Result<Option<String>, crate::RunnerError> {
    #[cfg(target_os = "linux")]
    {
        imp::store(operation, token, cancellation, environment)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (operation, token, cancellation, environment);
        Err(crate::RunnerError::catalog(
            crate::error::ErrorCode::CredentialStoreUnavailable,
        ))
    }
}

#[cfg(target_os = "linux")]
mod imp {
    use crate::error::ErrorCode;
    use crate::service::Environment;
    use crate::{CancellationToken, RunnerError};
    use std::ffi::{OsStr, OsString};
    use std::io::{Read, Write};
    use std::os::unix::fs::MetadataExt;
    use std::os::unix::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::{Command, Stdio};
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    /// The libsecret schema name `Bun.secrets` stamps on (and requires of) its items.
    pub(crate) const BUN_SCHEMA: &str = "com.oven-sh.bun.Secret";
    const ACCOUNT: &str = "api-key";
    const PROGRAM: &str = "secret-tool";
    const BOUND: Duration = Duration::from_secs(10);
    const PIPE_LIMIT: u64 = 8192;
    /// How long to wait for the output pipes to close after the tool's
    /// process group was killed.
    const DRAIN_GRACE: Duration = Duration::from_millis(500);
    /// Distribution locations searched before `PATH`, so a program earlier on
    /// `PATH` (for example a package manager's `node_modules/.bin`) cannot
    /// shadow the system tool and receive the key.
    const SYSTEM_DIRECTORIES: [&str; 4] = [
        "/usr/bin",
        "/bin",
        "/run/current-system/sw/bin",
        "/usr/local/bin",
    ];
    /// The only variables the tool inherits: how libsecret reaches the session bus.
    const BUS_VARIABLES: [&str; 2] = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"];

    fn unavailable() -> RunnerError {
        RunnerError::catalog(ErrorCode::CredentialStoreUnavailable)
    }

    fn protocol() -> RunnerError {
        RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
    }

    /// The strict key predicate (`rr_test_` + 32 lowercase hex). Only this closed
    /// alphabet is ever written to or accepted from the store.
    fn valid_token(token: &str) -> bool {
        token.strip_prefix("rr_test_").is_some_and(|v| {
            v.len() == 32
                && v.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    }

    /// Whether `path` (a file or directory, symlinks followed) is owned by root
    /// or the current user and is not group- or world-writable.
    fn trusted(path: &Path, uid: u32) -> bool {
        std::fs::metadata(path)
            .is_ok_and(|meta| (meta.uid() == 0 || meta.uid() == uid) && meta.mode() & 0o022 == 0)
    }

    /// An executable regular file that, after resolving symlinks, is trusted
    /// and sits in a trusted directory.
    fn acceptable(candidate: &Path, uid: u32) -> bool {
        use std::os::unix::fs::PermissionsExt;
        let Ok(real) = std::fs::canonicalize(candidate) else {
            return false;
        };
        std::fs::metadata(&real)
            .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
            && trusted(&real, uid)
            && real.parent().is_some_and(|parent| trusted(parent, uid))
    }

    /// Resolves `secret-tool`: the fixed `system` directories first, then
    /// absolute `PATH` entries (a relative entry would let the working
    /// directory supply the program). Every candidate must be trusted.
    fn find_program(path: Option<&OsStr>, system: &[PathBuf]) -> Option<PathBuf> {
        let uid = rustix::process::geteuid().as_raw();
        let from_path = path
            .map(|path| std::env::split_paths(path).collect::<Vec<_>>())
            .unwrap_or_default();
        system
            .iter()
            .cloned()
            .chain(from_path)
            .filter(|directory| directory.is_absolute())
            .map(|directory| directory.join(PROGRAM))
            .find(|candidate| acceptable(candidate, uid))
    }

    /// The system directories searched first. Test builds may replace them
    /// (`PROSE_TEST_SECRET_TOOL_SYSTEM_DIRS`, colon-separated, empty = none) so
    /// a fake tool on `PATH` is reachable on a host that has the real one.
    fn system_directories() -> Vec<PathBuf> {
        #[cfg(feature = "test-seams")]
        if let Some(value) = std::env::var_os("PROSE_TEST_SECRET_TOOL_SYSTEM_DIRS") {
            return std::env::split_paths(&value)
                .filter(|p| !p.as_os_str().is_empty())
                .collect();
        }
        SYSTEM_DIRECTORIES.iter().map(PathBuf::from).collect()
    }

    /// The inherited session-bus variables, or `None` when there is no way to
    /// reach a D-Bus session bus (nothing is spawned then).
    fn session_bus(
        lookup: impl Fn(&str) -> Option<OsString>,
    ) -> Option<Vec<(&'static str, OsString)>> {
        let bus: Vec<_> = BUS_VARIABLES
            .iter()
            .filter_map(|name| lookup(name).filter(|v| !v.is_empty()).map(|v| (*name, v)))
            .collect();
        (!bus.is_empty()).then_some(bus)
    }

    struct Tool {
        program: PathBuf,
        bus: Vec<(&'static str, OsString)>,
        bound: Duration,
    }

    struct Finished {
        success: bool,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    }

    impl Tool {
        fn execute(
            &self,
            operation: &str,
            token: Option<&str>,
            service: &str,
            cancellation: &CancellationToken,
        ) -> Result<Option<String>, RunnerError> {
            if cancellation.is_cancelled() {
                return Err(RunnerError::catalog(ErrorCode::Cancelled));
            }
            let label = format!("--label={service}/{ACCOUNT}");
            let attributes = [
                "xdg:schema",
                BUN_SCHEMA,
                "service",
                service,
                "account",
                ACCOUNT,
            ];
            match operation {
                "get" => {
                    let done = self.run(&["lookup"], &attributes, None, cancellation)?;
                    if !done.success {
                        // `lookup` exits 1 silently when nothing matches; any
                        // diagnostic means the Secret Service itself failed.
                        return if done.stderr.is_empty() && done.stdout.is_empty() {
                            Ok(None)
                        } else {
                            Err(unavailable())
                        };
                    }
                    // Transport only: the value is returned exactly as stored
                    // (as `Bun.secrets.get` does); callers validate it. Invalid
                    // UTF-8 cannot be a key and is kept invalid, not dropped.
                    Ok(Some(String::from_utf8_lossy(&done.stdout).into_owned()))
                }
                "set" => {
                    let token = token.filter(|t| valid_token(t)).ok_or_else(protocol)?;
                    let done =
                        self.run(&["store", &label], &attributes, Some(token), cancellation)?;
                    if done.success && done.stderr.is_empty() {
                        Ok(None)
                    } else {
                        Err(unavailable())
                    }
                }
                "delete" => {
                    let done = self.run(&["clear"], &attributes, None, cancellation)?;
                    // `clear` also exits 1 silently when nothing matched.
                    if done.success || done.stderr.is_empty() {
                        Ok(None)
                    } else {
                        Err(unavailable())
                    }
                }
                _ => Err(unavailable()),
            }
        }

        fn run(
            &self,
            command: &[&str],
            attributes: &[&str],
            input: Option<&str>,
            cancellation: &CancellationToken,
        ) -> Result<Finished, RunnerError> {
            let mut process = Command::new(&self.program);
            process
                .args(command)
                .args(attributes)
                .env_clear()
                .envs(self.bus.iter().map(|(k, v)| (k, v)))
                .current_dir("/")
                // Its own process group, so a timeout or cancellation kills
                // every descendant that could hold the output pipes open.
                .process_group(0)
                .stdin(if input.is_some() {
                    Stdio::piped()
                } else {
                    Stdio::null()
                })
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let mut child = process.spawn().map_err(|_| unavailable())?;
            let group = rustix::process::Pid::from_child(&child);
            let kill_group = || {
                let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
            };
            // Readers report over a channel so they are never joined: a pipe
            // held open by an escaped descendant cannot block the caller.
            let (sender, receiver) = mpsc::channel();
            let read = |index: usize, pipe: Box<dyn Read + Send>| {
                let sender = sender.clone();
                std::thread::spawn(move || {
                    let mut bytes = Vec::new();
                    let ok = pipe.take(PIPE_LIMIT + 1).read_to_end(&mut bytes).is_ok();
                    let _ = sender.send((index, ok && bytes.len() as u64 <= PIPE_LIMIT, bytes));
                });
            };
            read(0, Box::new(child.stdout.take().expect("piped stdout")));
            read(1, Box::new(child.stderr.take().expect("piped stderr")));
            drop(sender);
            // The secret travels only over this anonymous pipe, without a newline
            // (`secret-tool store` keeps every byte it reads).
            let wrote = match (input, child.stdin.take()) {
                (Some(secret), Some(mut stdin)) => stdin.write_all(secret.as_bytes()).is_ok(),
                _ => true,
            };
            let deadline = Instant::now() + self.bound;
            let mut status = None;
            let mut cancelled = false;
            if wrote {
                loop {
                    if cancellation.is_cancelled() {
                        cancelled = true;
                        break;
                    }
                    if Instant::now() >= deadline {
                        break;
                    }
                    match child.try_wait() {
                        Ok(Some(exit)) => {
                            status = Some(exit);
                            break;
                        }
                        Ok(None) => std::thread::sleep(Duration::from_millis(20)),
                        Err(_) => break,
                    }
                }
            }
            if status.is_none() {
                // Kill the group before reaping, so its id cannot be reused.
                kill_group();
                let _ = child.kill();
                let _ = child.wait();
            }
            // Collect both streams, bounded by the call's deadline (or a short
            // grace after a kill) and interruptible by cancellation.
            let drain_until = if status.is_some() {
                deadline
            } else {
                Instant::now() + DRAIN_GRACE
            };
            let mut streams: [Option<(bool, Vec<u8>)>; 2] = [None, None];
            while streams.iter().any(Option::is_none) {
                if !cancelled && cancellation.is_cancelled() {
                    cancelled = true;
                }
                let now = Instant::now();
                if (cancelled && status.is_some()) || now >= drain_until {
                    // A descendant still holds a pipe: kill what remains of the
                    // group and give up on the output.
                    kill_group();
                    break;
                }
                let wait = (drain_until - now).min(Duration::from_millis(20));
                match receiver.recv_timeout(wait) {
                    Ok((index, ok, bytes)) => streams[index] = Some((ok, bytes)),
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }
            if cancelled {
                return Err(RunnerError::catalog(ErrorCode::Cancelled));
            }
            let status = status.ok_or_else(unavailable)?;
            let [Some((out_ok, stdout)), Some((err_ok, stderr))] = streams else {
                return Err(unavailable());
            };
            if !out_ok || !err_ok {
                return Err(unavailable());
            }
            // Only a plain exit code counts; a signal is a failure.
            match status.code() {
                Some(0) => Ok(Finished {
                    success: true,
                    stdout,
                    stderr,
                }),
                Some(1) => Ok(Finished {
                    success: false,
                    stdout,
                    stderr,
                }),
                _ => Err(unavailable()),
            }
        }
    }

    pub(super) fn store(
        operation: &str,
        token: Option<&str>,
        cancellation: &CancellationToken,
        environment: &Environment,
    ) -> Result<Option<String>, RunnerError> {
        let service = environment.store_service.as_str();
        let tool = Tool {
            program: find_program(std::env::var_os("PATH").as_deref(), &system_directories())
                .ok_or_else(unavailable)?,
            bus: session_bus(|name| std::env::var_os(name)).ok_or_else(unavailable)?,
            bound: BOUND,
        };
        if cancellation.is_cancelled() {
            return Err(RunnerError::catalog(ErrorCode::Cancelled));
        }
        // A cancelled write may or may not have reached the store.
        tool.execute(operation, token, service, cancellation)
            .map_err(|error| {
                if operation == "set" && error.code == ErrorCode::Cancelled {
                    super::macos::outcome_unknown(environment.credential_env)
                } else {
                    error
                }
            })
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use std::path::Path;
        use tempfile::TempDir;

        const TOKEN: &str = "rr_test_0123456789abcdef0123456789abcdef";
        const SERVICE: &str = "org.openprose.cli.production";

        /// A fake `secret-tool`: records argv, environment and stdin, and keeps one
        /// item in a state file. `mode` selects failure behavior. Every path is
        /// absolute because the real tool runs with no `PATH`.
        struct Fake {
            dir: TempDir,
        }

        impl Fake {
            fn new() -> Self {
                let dir = TempDir::new().unwrap();
                let root = dir.path().display().to_string();
                fs::create_dir(dir.path().join("bin")).unwrap();
                let script = format!(
                    r#"#!/bin/sh
R='{root}'
printf '%s\n' "$@" > "$R/cmdline"
/usr/bin/tr '\0' '\n' < /proc/$$/environ > "$R/env"
echo x >> "$R/calls"
mode=$(/bin/cat "$R/mode" 2>/dev/null)
case "$mode" in
  fail) echo 'secret-tool: Could not connect: No such file or directory' >&2; exit 1 ;;
  sleep) exec /bin/sleep 30 ;;
  grandchild) /bin/sleep 30 & /bin/sleep 30 ;;
  escape) /bin/sleep 30 & echo "$!" > "$R/escaped"; exit 0 ;;
  garbage) printf 'rr_test_NOT-A-KEY'; exit 0 ;;
  huge) /usr/bin/head -c 20000 /dev/zero; exit 0 ;;
  signal) kill -9 $$ ;;
esac
case "$1" in
  store) /bin/cat > "$R/stdin"; /bin/cp "$R/stdin" "$R/item"; exit 0 ;;
  lookup) [ -f "$R/item" ] || exit 1; /bin/cat "$R/item"; exit 0 ;;
  clear) [ -f "$R/item" ] || exit 1; /bin/rm "$R/item"; exit 0 ;;
esac
exit 2
"#
                );
                let program = dir.path().join("bin").join(PROGRAM);
                fs::write(&program, script).unwrap();
                fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
                Self { dir }
            }
            fn tool(&self) -> Tool {
                Tool {
                    program: self.dir.path().join("bin").join(PROGRAM),
                    bus: vec![
                        ("DBUS_SESSION_BUS_ADDRESS", "unix:path=/fake/bus".into()),
                        ("XDG_RUNTIME_DIR", "/fake/run".into()),
                    ],
                    bound: Duration::from_secs(10),
                }
            }
            fn mode(&self, mode: &str) {
                fs::write(self.dir.path().join("mode"), mode).unwrap();
            }
            fn read(&self, name: &str) -> String {
                fs::read_to_string(self.dir.path().join(name)).unwrap_or_default()
            }
            fn calls(&self) -> usize {
                self.read("calls").lines().count()
            }
        }

        fn run(
            tool: &Tool,
            operation: &str,
            token: Option<&str>,
        ) -> Result<Option<String>, RunnerError> {
            tool.execute(operation, token, SERVICE, &CancellationToken::default())
        }

        fn attributes() -> String {
            format!("xdg:schema\n{BUN_SCHEMA}\nservice\n{SERVICE}\naccount\napi-key\n")
        }

        #[test]
        fn credential_store_round_trip_uses_fixed_argv_stdin_and_cleared_environment() {
            let fake = Fake::new();
            let tool = fake.tool();
            assert!(run(&tool, "get", None).unwrap().is_none());
            assert_eq!(fake.read("cmdline"), format!("lookup\n{}", attributes()));

            assert!(run(&tool, "set", Some(TOKEN)).unwrap().is_none());
            let argv = fake.read("cmdline");
            assert_eq!(
                argv,
                format!("store\n--label={SERVICE}/api-key\n{}", attributes())
            );
            assert!(!argv.contains("rr_test_"), "the secret never enters argv");
            assert_eq!(fake.read("stdin"), TOKEN, "secret on stdin, no newline");
            let mut environment: Vec<_> = fake.read("env").lines().map(str::to_owned).collect();
            environment.sort();
            assert_eq!(
                environment,
                [
                    "DBUS_SESSION_BUS_ADDRESS=unix:path=/fake/bus",
                    "XDG_RUNTIME_DIR=/fake/run"
                ]
            );

            assert_eq!(run(&tool, "get", None).unwrap().as_deref(), Some(TOKEN));
            assert!(run(&tool, "delete", None).unwrap().is_none());
            assert_eq!(fake.read("cmdline"), format!("clear\n{}", attributes()));
            assert!(run(&tool, "get", None).unwrap().is_none());
            // Deleting a missing item is not an error (logout is idempotent).
            assert!(run(&tool, "delete", None).unwrap().is_none());
        }

        #[test]
        fn credential_store_lookup_returns_the_stored_value_unchanged() {
            // Transport only, like `Bun.secrets.get`: a trailing newline is
            // kept (callers then reject the value exactly as Bun does).
            let fake = Fake::new();
            fs::write(fake.dir.path().join("item"), format!("{TOKEN}\n")).unwrap();
            assert_eq!(
                run(&fake.tool(), "get", None).unwrap(),
                Some(format!("{TOKEN}\n"))
            );
            fs::write(fake.dir.path().join("item"), "garbage-value").unwrap();
            assert_eq!(
                run(&fake.tool(), "get", None).unwrap().as_deref(),
                Some("garbage-value")
            );
        }

        #[test]
        fn credential_store_missing_tool_is_unavailable() {
            let empty = TempDir::new().unwrap();
            assert!(find_program(Some(empty.path().as_os_str()), &[]).is_none());
            assert!(find_program(None, &[]).is_none());
            let fake = Fake::new();
            let bin = fake.dir.path().join("bin");
            assert_eq!(
                find_program(Some(bin.as_os_str()), &[]),
                Some(bin.join(PROGRAM))
            );
            // A relative PATH entry is never searched.
            let relative = std::env::join_paths([Path::new("bin")]).unwrap();
            assert!(find_program(Some(&relative), &[]).is_none());
            // A non-executable file is not a program.
            let plain = TempDir::new().unwrap();
            fs::write(plain.path().join(PROGRAM), "").unwrap();
            assert!(find_program(Some(plain.path().as_os_str()), &[]).is_none());
            // A program that cannot be spawned is unavailable too.
            let tool = Tool {
                program: empty.path().join(PROGRAM),
                ..fake.tool()
            };
            assert_eq!(
                run(&tool, "get", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
        }

        #[test]
        fn credential_store_prefers_system_directories_and_rejects_untrusted_programs() {
            let system = Fake::new();
            let shadow = Fake::new();
            let system_bin = system.dir.path().join("bin");
            let shadow_bin = shadow.dir.path().join("bin");
            // A system directory wins over an earlier PATH entry.
            assert_eq!(
                find_program(Some(shadow_bin.as_os_str()), &[system_bin.clone()]),
                Some(system_bin.join(PROGRAM))
            );
            // A group- or world-writable program or directory is never trusted.
            let program = shadow_bin.join(PROGRAM);
            fs::set_permissions(&program, fs::Permissions::from_mode(0o775)).unwrap();
            assert!(find_program(Some(shadow_bin.as_os_str()), &[]).is_none());
            fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
            fs::set_permissions(&shadow_bin, fs::Permissions::from_mode(0o777)).unwrap();
            assert!(find_program(Some(shadow_bin.as_os_str()), &[]).is_none());
            // A symlink is judged by its target's file and directory.
            fs::set_permissions(&shadow_bin, fs::Permissions::from_mode(0o755)).unwrap();
            let links = TempDir::new().unwrap();
            std::os::unix::fs::symlink(&program, links.path().join(PROGRAM)).unwrap();
            assert_eq!(
                find_program(Some(links.path().as_os_str()), &[]),
                Some(links.path().join(PROGRAM))
            );
            fs::set_permissions(&shadow_bin, fs::Permissions::from_mode(0o777)).unwrap();
            assert!(find_program(Some(links.path().as_os_str()), &[]).is_none());
            fs::set_permissions(&shadow_bin, fs::Permissions::from_mode(0o755)).unwrap();
            // The default system list covers the distribution locations.
            assert_eq!(SYSTEM_DIRECTORIES[0], "/usr/bin");
        }

        #[test]
        fn credential_store_without_dbus_is_unavailable() {
            assert!(session_bus(|_| None).is_none());
            assert!(session_bus(|_| Some(OsString::new())).is_none());
            let only_runtime = session_bus(|name| {
                (name == "XDG_RUNTIME_DIR").then(|| OsString::from("/run/user/1"))
            })
            .unwrap();
            assert_eq!(
                only_runtime,
                vec![("XDG_RUNTIME_DIR", OsString::from("/run/user/1"))]
            );
            // The tool's own D-Bus failure (exit 1 with a diagnostic) is unavailable,
            // never "no key".
            let fake = Fake::new();
            fake.mode("fail");
            for operation in ["get", "delete"] {
                assert_eq!(
                    run(&fake.tool(), operation, None).unwrap_err().code,
                    ErrorCode::CredentialStoreUnavailable
                );
            }
            assert_eq!(
                run(&fake.tool(), "set", Some(TOKEN)).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
        }

        #[test]
        fn credential_store_rejects_tokens_outside_the_key_alphabet() {
            let fake = Fake::new();
            let upper = format!("rr_test_{}", "A".repeat(32));
            let short = format!("rr_test_{}", "a".repeat(31));
            let newline = format!("{TOKEN}\nservice evil");
            let shell = format!("rr_test_$(touch {}/pwned)", fake.dir.path().display());
            for bad in [
                "",
                "rr_live_0123456789abcdef0123456789abcdef",
                upper.as_str(),
                short.as_str(),
                newline.as_str(),
                shell.as_str(),
            ] {
                assert_eq!(
                    run(&fake.tool(), "set", Some(bad)).unwrap_err().code,
                    ErrorCode::ServiceProtocolInvalid
                );
            }
            assert_eq!(
                run(&fake.tool(), "set", None).unwrap_err().code,
                ErrorCode::ServiceProtocolInvalid
            );
            assert_eq!(fake.calls(), 0, "an invalid token never reaches the tool");
            // A malformed stored value is returned as is; callers reject it.
            fake.mode("garbage");
            assert_eq!(
                run(&fake.tool(), "get", None).unwrap().as_deref(),
                Some("rr_test_NOT-A-KEY")
            );
        }

        #[test]
        fn credential_store_bounds_output_signals_and_unknown_operations() {
            let fake = Fake::new();
            fake.mode("huge");
            assert_eq!(
                run(&fake.tool(), "get", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
            fake.mode("signal");
            assert_eq!(
                run(&fake.tool(), "get", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
            assert_eq!(
                run(&fake.tool(), "list", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
        }

        #[test]
        fn credential_store_times_out_after_the_bound() {
            let fake = Fake::new();
            fake.mode("sleep");
            let tool = Tool {
                bound: Duration::from_millis(300),
                ..fake.tool()
            };
            let began = Instant::now();
            assert_eq!(
                run(&tool, "get", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
            assert!(began.elapsed() < Duration::from_secs(5));
        }

        #[test]
        fn credential_store_bound_holds_when_a_descendant_keeps_the_pipes_open() {
            let fake = Fake::new();
            fake.mode("grandchild");
            let tool = Tool {
                bound: Duration::from_millis(300),
                ..fake.tool()
            };
            let began = Instant::now();
            assert_eq!(
                run(&tool, "get", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
            assert!(
                began.elapsed() < Duration::from_secs(3),
                "{:?}",
                began.elapsed()
            );
            // Cancellation is prompt too.
            let cancellation = CancellationToken::default();
            let trigger = cancellation.clone();
            let canceller = std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(200));
                trigger.cancel();
            });
            let began = Instant::now();
            let error = fake
                .tool()
                .execute("get", None, SERVICE, &cancellation)
                .unwrap_err();
            canceller.join().unwrap();
            assert_eq!(error.code, ErrorCode::Cancelled);
            assert!(
                began.elapsed() < Duration::from_secs(3),
                "{:?}",
                began.elapsed()
            );
        }

        #[test]
        fn credential_store_exit_with_a_lingering_descendant_is_bounded_and_killed() {
            // The tool exits 0 but leaves a child holding stdout: the call
            // ends at the bound and the whole group is killed.
            let fake = Fake::new();
            fake.mode("escape");
            let tool = Tool {
                bound: Duration::from_millis(300),
                ..fake.tool()
            };
            let began = Instant::now();
            assert_eq!(
                run(&tool, "get", None).unwrap_err().code,
                ErrorCode::CredentialStoreUnavailable
            );
            assert!(
                began.elapsed() < Duration::from_secs(3),
                "{:?}",
                began.elapsed()
            );
            let pid = fake.read("escaped");
            let pid = pid.trim();
            std::thread::sleep(Duration::from_millis(100));
            let state = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
            // Gone, or a zombie awaiting its (dead) parent's reaper.
            assert!(
                state.is_empty() || state.split(") ").nth(1).is_some_and(|s| s.starts_with('Z')),
                "descendant survived: {state}"
            );
        }

        #[test]
        fn credential_store_cancellation_kills_the_tool_promptly() {
            let fake = Fake::new();
            fake.mode("sleep");
            let cancellation = CancellationToken::default();
            let trigger = cancellation.clone();
            let canceller = std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(200));
                trigger.cancel();
            });
            let began = Instant::now();
            let error = fake
                .tool()
                .execute("set", Some(TOKEN), SERVICE, &cancellation)
                .unwrap_err();
            canceller.join().unwrap();
            assert_eq!(error.code, ErrorCode::Cancelled);
            assert!(began.elapsed() < Duration::from_secs(3));
            // Already cancelled: nothing is spawned.
            let calls = fake.calls();
            assert_eq!(
                fake.tool()
                    .execute("get", None, SERVICE, &cancellation)
                    .unwrap_err()
                    .code,
                ErrorCode::Cancelled
            );
            assert_eq!(fake.calls(), calls);
        }

        /// One step of the opt-in Rust/Bun interop check driven by
        /// `cli/bun/test/credential-store-interop.test.ts` against the real
        /// Secret Service. Without `PROSE_TEST_REAL_KEYRING=1` it does nothing.
        /// It only ever touches `PROSE_TEST_KEYRING_SERVICE` (which must be
        /// `org.openprose.cli.test`) and never prints the secret.
        #[test]
        fn credential_store_real_keyring_step() {
            if std::env::var("PROSE_TEST_REAL_KEYRING").as_deref() != Ok("1") {
                return;
            }
            let service = std::env::var("PROSE_TEST_KEYRING_SERVICE").unwrap();
            assert_eq!(
                service, "org.openprose.cli.test",
                "refusing to touch a real service"
            );
            let step = std::env::var("PROSE_TEST_KEYRING_STEP").unwrap();
            let expected = std::env::var("PROSE_TEST_KEYRING_TOKEN").ok();
            let tool = Tool {
                program: find_program(std::env::var_os("PATH").as_deref(), &system_directories())
                    .expect("secret-tool"),
                bus: session_bus(|name| std::env::var_os(name)).expect("session bus"),
                bound: BOUND,
            };
            let cancellation = CancellationToken::default();
            let result = tool
                .execute(&step, expected.as_deref(), &service, &cancellation)
                .map_err(|e| e.code);
            match step.as_str() {
                "get" => {
                    let found = result.expect("lookup");
                    assert!(
                        found == expected,
                        "Rust read a different value than Bun wrote"
                    );
                }
                "set" | "delete" => assert!(result.expect("mutation").is_none()),
                other => panic!("unknown step {other}"),
            }
        }
    }
}

/// macOS login keychain through `/usr/bin/security -i` (see
/// `docs/service/credentials.md` and the shared
/// `shared/fixtures/credentials/macos-security.v1.json`, which both ports test
/// against).
///
/// The item is created by the `security` tool itself, so its access list
/// trusts `/usr/bin/security` and every build of either port reads it without
/// a keychain prompt. An item left by an earlier build (without the comment
/// marker) is re-created through `security` after one successful read.
#[cfg(unix)]
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) mod macos {
    use crate::error::ErrorCode;
    use crate::{CancellationToken, RunnerError};
    use std::io::{Read, Write};
    use std::os::unix::process::CommandExt;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    pub(crate) const PROGRAM: &str = "/usr/bin/security";
    pub(crate) const ACCOUNT: &str = "api-key";
    /// The comment every item written by this CLI carries.
    pub(crate) const COMMENT: &str = "openprose-cli-v1";
    const BOUND: Duration = Duration::from_secs(10);
    const OUTPUT_LIMIT: usize = 8192;
    const ABSENT: [&str; 2] = ["could not be found", "(-25300)"];
    const DENIED: [&str; 4] = [
        "User canceled",
        "(-128)",
        "user name or passphrase you entered is not correct",
        "(-25293)",
    ];
    const INTERACTION: [&str; 2] = ["User interaction is not allowed", "(-25308)"];
    const FAILURE: [&str; 3] = ["SecKeychain", "SecItem", "error:"];

    /// How one `security -i` session ended.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub(crate) enum Class {
        Absent,
        Denied,
        InteractionNotAllowed,
        Unavailable,
        Ok,
    }

    /// A finished session, or why it did not finish.
    #[derive(Debug)]
    pub(crate) enum Session {
        Finished {
            exit: i32,
            stdout: Vec<u8>,
            stderr: Vec<u8>,
        },
        /// Spawn failure, signal, time bound or output over the limit.
        Failed,
        Cancelled,
    }

    /// Classifies a finished session by its standard error, then its exit status.
    pub(crate) fn classify(exit: i32, stderr: &str) -> Class {
        let has = |markers: &[&str]| markers.iter().any(|marker| stderr.contains(marker));
        if has(&ABSENT) {
            Class::Absent
        } else if has(&DENIED) {
            Class::Denied
        } else if has(&INTERACTION) {
            Class::InteractionNotAllowed
        } else if has(&FAILURE) || exit != 0 {
            Class::Unavailable
        } else {
            Class::Ok
        }
    }

    pub(crate) fn probe_script(service: &str) -> String {
        format!("find-generic-password -s {service} -a {ACCOUNT}\n")
    }
    pub(crate) fn read_script(service: &str) -> String {
        format!("find-generic-password -s {service} -a {ACCOUNT} -w\n")
    }
    pub(crate) fn remove_script(service: &str) -> String {
        format!("delete-generic-password -s {service} -a {ACCOUNT}\n")
    }
    pub(crate) fn add_script(service: &str, token: &str) -> String {
        format!("add-generic-password -U -s {service} -a {ACCOUNT} -j {COMMENT} -w {token}\n")
    }
    fn owned_marker() -> String {
        format!("\"icmt\"<blob>=\"{COMMENT}\"")
    }

    fn valid_service(service: &str) -> bool {
        !service.is_empty()
            && service
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b".-".contains(&b))
    }

    fn valid_token(token: &str) -> bool {
        token.strip_prefix("rr_test_").is_some_and(|v| {
            v.len() == 32
                && v.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    }

    fn unavailable() -> RunnerError {
        RunnerError::catalog(ErrorCode::CredentialStoreUnavailable)
    }

    /// The keychain refused to read an earlier build's item without a prompt.
    pub(crate) fn keychain_access(variable: &str) -> RunnerError {
        let mut error = unavailable().with_detail(
            "reason",
            format!(
                "the macOS keychain needs approval in a prompt before this build can read the stored key (the key was saved by an earlier build of the CLI); set {variable} for this command"
            ),
        );
        error.action = format!(
            "Run the command in a desktop session and allow access in the macOS keychain prompt once, or set {variable} for this command."
        );
        error
    }

    /// A cancelled write whose outcome the store never confirmed.
    pub(crate) fn outcome_unknown(variable: &str) -> RunnerError {
        let mut error = unavailable().with_detail(
            "reason",
            format!(
                "the command was cancelled before the operating system credential store confirmed the change, so it is unknown whether the key was stored; set {variable} for this command"
            ),
        );
        error.action = format!(
            "Set {variable} for this command, or unlock or configure the operating system credential store, then retry."
        );
        error
    }

    /// Runs `get`, `set` or `delete` over `run` (one `security -i` session per
    /// call). `get` and `delete` report a missing item as `Ok(None)`.
    pub(crate) fn operate(
        run: &mut dyn FnMut(&str) -> Session,
        operation: &str,
        token: Option<&str>,
        service: &str,
        variable: &str,
    ) -> Result<Option<String>, RunnerError> {
        if !valid_service(service) {
            return Err(unavailable());
        }
        let writing = operation == "set";
        let mut step = |script: &str| -> Result<(Class, String, String), RunnerError> {
            match run(script) {
                Session::Cancelled if writing => Err(outcome_unknown(variable)),
                Session::Cancelled => Err(RunnerError::catalog(ErrorCode::Cancelled)),
                Session::Failed => Err(unavailable()),
                Session::Finished {
                    exit,
                    stdout,
                    stderr,
                } => {
                    let stderr = String::from_utf8_lossy(&stderr).into_owned();
                    let stdout = String::from_utf8_lossy(&stdout).into_owned();
                    Ok((classify(exit, &stderr), stdout, stderr))
                }
            }
        };
        let failure = |class: Class| match class {
            Class::Denied => RunnerError::catalog(ErrorCode::Cancelled),
            Class::InteractionNotAllowed => keychain_access(variable),
            _ => unavailable(),
        };
        match operation {
            "get" => {
                let (class, stdout, stderr) = step(&probe_script(service))?;
                match class {
                    Class::Absent => return Ok(None),
                    Class::Ok => {}
                    other => return Err(failure(other)),
                }
                let owned = stdout.contains(&owned_marker()) || stderr.contains(&owned_marker());
                let (class, value, _) = step(&read_script(service))?;
                match class {
                    Class::Absent => return Ok(None),
                    Class::Ok => {}
                    other => return Err(failure(other)),
                }
                let value = value.trim_matches([' ', '\t', '\r', '\n']).to_owned();
                if !owned && valid_token(&value) {
                    // Best effort: re-create the item through `security`, so
                    // later reads by any build never prompt.
                    let migrate =
                        format!("{}{}", remove_script(service), add_script(service, &value));
                    let _ = run(&migrate);
                }
                Ok(Some(value))
            }
            "set" => {
                let token = token
                    .filter(|t| valid_token(t))
                    .ok_or_else(|| RunnerError::catalog(ErrorCode::ServiceProtocolInvalid))?;
                match step(&remove_script(service))?.0 {
                    Class::Absent | Class::Ok => {}
                    other => return Err(failure(other)),
                }
                match step(&add_script(service, token))?.0 {
                    Class::Ok => Ok(None),
                    Class::Absent => Err(unavailable()),
                    other => Err(failure(other)),
                }
            }
            "delete" => match step(&remove_script(service))?.0 {
                Class::Absent | Class::Ok => Ok(None),
                other => Err(failure(other)),
            },
            _ => Err(unavailable()),
        }
    }

    /// The program: `/usr/bin/security`, or (test builds only) the
    /// `PROSE_TEST_MACOS_SECURITY` override.
    fn program() -> PathBuf {
        #[cfg(feature = "test-seams")]
        if let Some(value) = std::env::var_os("PROSE_TEST_MACOS_SECURITY") {
            return PathBuf::from(value);
        }
        PathBuf::from(PROGRAM)
    }

    /// One `security -i` session: the script on standard input, an empty
    /// environment, its own process group, a time bound and bounded output.
    pub(crate) fn spawn(
        program: &std::path::Path,
        script: &str,
        bound: Duration,
        cancellation: &CancellationToken,
    ) -> Session {
        if cancellation.is_cancelled() {
            return Session::Cancelled;
        }
        let Ok(mut child) = Command::new(program)
            .arg("-i")
            .env_clear()
            .current_dir("/")
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        else {
            return Session::Failed;
        };
        let group = rustix::process::Pid::from_child(&child);
        let kill_group = || {
            let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
        };
        let (sender, receiver) = mpsc::channel();
        let read = |index: usize, pipe: Box<dyn Read + Send>| {
            let sender = sender.clone();
            std::thread::spawn(move || {
                let mut bytes = Vec::new();
                let ok = pipe
                    .take(OUTPUT_LIMIT as u64 + 1)
                    .read_to_end(&mut bytes)
                    .is_ok();
                let _ = sender.send((index, ok && bytes.len() <= OUTPUT_LIMIT, bytes));
            });
        };
        read(0, Box::new(child.stdout.take().expect("piped stdout")));
        read(1, Box::new(child.stderr.take().expect("piped stderr")));
        drop(sender);
        let wrote = child
            .stdin
            .take()
            .is_some_and(|mut stdin| stdin.write_all(script.as_bytes()).is_ok());
        let deadline = Instant::now() + bound;
        let mut status = None;
        let mut cancelled = false;
        if wrote {
            loop {
                if cancellation.is_cancelled() {
                    cancelled = true;
                    break;
                }
                if Instant::now() >= deadline {
                    break;
                }
                match child.try_wait() {
                    Ok(Some(exit)) => {
                        status = Some(exit);
                        break;
                    }
                    Ok(None) => std::thread::sleep(Duration::from_millis(10)),
                    Err(_) => break,
                }
            }
        }
        if status.is_none() {
            kill_group();
            let _ = child.kill();
            let _ = child.wait();
        }
        if cancelled {
            return Session::Cancelled;
        }
        let drain_until = if status.is_some() {
            deadline
        } else {
            Instant::now() + Duration::from_millis(500)
        };
        let mut streams: [Option<(bool, Vec<u8>)>; 2] = [None, None];
        while streams.iter().any(Option::is_none) {
            if cancellation.is_cancelled() {
                kill_group();
                return Session::Cancelled;
            }
            let now = Instant::now();
            if now >= drain_until {
                kill_group();
                break;
            }
            match receiver.recv_timeout((drain_until - now).min(Duration::from_millis(20))) {
                Ok((index, ok, bytes)) => streams[index] = Some((ok, bytes)),
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        let (Some(status), [Some((true, stdout)), Some((true, stderr))]) = (status, streams) else {
            return Session::Failed;
        };
        // Only a plain exit status counts; a signal is a failure.
        match status.code() {
            Some(exit) => Session::Finished {
                exit,
                stdout,
                stderr,
            },
            None => Session::Failed,
        }
    }

    /// `get`, `set` or `delete` the service's API key in the login keychain.
    pub(crate) fn store(
        operation: &str,
        token: Option<&str>,
        cancellation: &CancellationToken,
        environment: &crate::service::Environment,
    ) -> Result<Option<String>, RunnerError> {
        if cancellation.is_cancelled() {
            return Err(RunnerError::catalog(ErrorCode::Cancelled));
        }
        let program = program();
        operate(
            &mut |script| spawn(&program, script, BOUND, cancellation),
            operation,
            token,
            &environment.store_service,
            environment.credential_env,
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use serde_json::Value;
        use std::fs;
        use std::os::unix::fs::PermissionsExt;
        use tempfile::TempDir;

        fn fixture() -> Value {
            serde_json::from_str(include_str!(
                "../../../../shared/fixtures/credentials/macos-security.v1.json"
            ))
            .unwrap()
        }

        #[test]
        fn macos_store_constants_match_the_shared_fixture() {
            let f = fixture();
            assert_eq!(f["program"], PROGRAM);
            assert_eq!(f["argv"], serde_json::json!(["-i"]));
            assert_eq!(f["account"], ACCOUNT);
            assert_eq!(f["comment"], COMMENT);
            assert_eq!(f["ownedMarker"], owned_marker());
            assert_eq!(f["boundMs"], u64::try_from(BOUND.as_millis()).unwrap());
            assert_eq!(f["outputLimitBytes"], OUTPUT_LIMIT);
            let scripts = &f["scripts"];
            let service = "{service}";
            assert_eq!(scripts["probe"], probe_script(service));
            assert_eq!(scripts["read"], read_script(service));
            assert_eq!(scripts["remove"], remove_script(service));
            assert_eq!(scripts["add"], add_script(service, "{token}"));
            assert_eq!(
                scripts["migrate"],
                format!(
                    "{}{}",
                    remove_script(service),
                    add_script(service, "{token}")
                )
            );
            let classes = &f["classification"];
            for (name, markers) in [
                ("absent", &ABSENT[..]),
                ("denied", &DENIED[..]),
                ("interaction-not-allowed", &INTERACTION[..]),
                ("unavailable", &FAILURE[..]),
            ] {
                assert_eq!(classes[name], serde_json::json!(markers), "{name}");
            }
            let variable = "OPENPROSE_API_KEY";
            for (name, error) in [
                ("keychain-access", keychain_access(variable)),
                ("outcome-unknown", outcome_unknown(variable)),
            ] {
                let reasons = &f["reasons"][name];
                let fill = |text: &Value| text.as_str().unwrap().replace("{variable}", variable);
                assert_eq!(
                    error.details.as_ref().unwrap()["reason"],
                    fill(&reasons["reason"])
                );
                assert_eq!(error.action, fill(&reasons["action"]));
            }
        }

        /// Checks one outcome against a scenario's `expected`.
        fn check(id: &str, expected: &Value, result: &Result<Option<String>, RunnerError>) {
            let variable = "OPENPROSE_API_KEY";
            if let Some(value) = expected.get("value") {
                assert_eq!(result.as_ref().unwrap().as_deref(), value.as_str(), "{id}");
            } else if expected.get("absent").is_some() || expected.get("ok").is_some() {
                assert_eq!(result.as_ref().unwrap(), &None, "{id}");
            } else {
                let error = result.as_ref().unwrap_err();
                let code = serde_json::to_value(error.code).unwrap();
                assert_eq!(code, expected["error"], "{id}");
                let want = match expected["reason"].as_str() {
                    Some("keychain-access") => keychain_access(variable).details,
                    Some("outcome-unknown") => outcome_unknown(variable).details,
                    _ => None,
                };
                assert_eq!(error.details, want, "{id}");
            }
        }

        #[test]
        fn macos_store_follows_every_shared_scenario() {
            let f = fixture();
            let service = f["service"].as_str().unwrap();
            for scenario in f["scenarios"].as_array().unwrap() {
                let id = scenario["id"].as_str().unwrap();
                let mut responses = scenario["responses"].as_array().unwrap().iter();
                let mut scripts = Vec::new();
                let result = operate(
                    &mut |script| {
                        scripts.push(script.to_owned());
                        let r = responses
                            .next()
                            .unwrap_or_else(|| panic!("{id}: extra session"));
                        Session::Finished {
                            exit: i32::try_from(r["exitCode"].as_i64().unwrap()).unwrap(),
                            stdout: r["stdout"].as_str().unwrap().as_bytes().to_vec(),
                            stderr: r["stderr"].as_str().unwrap().as_bytes().to_vec(),
                        }
                    },
                    scenario["operation"].as_str().unwrap(),
                    scenario["token"].as_str(),
                    service,
                    "OPENPROSE_API_KEY",
                );
                assert_eq!(
                    serde_json::json!(scripts),
                    scenario["expectedScripts"],
                    "{id}"
                );
                check(id, &scenario["expected"], &result);
            }
        }

        #[test]
        fn macos_store_cancellation_is_cancelled_except_while_writing() {
            for (operation, token, want) in [
                ("get", None, "CANCELLED"),
                ("delete", None, "CANCELLED"),
                (
                    "set",
                    Some("rr_test_0123456789abcdef0123456789abcdef"),
                    "CREDENTIAL_STORE_UNAVAILABLE",
                ),
            ] {
                let error = operate(
                    &mut |_| Session::Cancelled,
                    operation,
                    token,
                    "org.openprose.cli.production",
                    "OPENPROSE_API_KEY",
                )
                .unwrap_err();
                assert_eq!(serde_json::to_value(error.code).unwrap(), want);
                if operation == "set" {
                    assert_eq!(error.details, outcome_unknown("OPENPROSE_API_KEY").details);
                }
            }
            // An invalid service never reaches the tool.
            assert!(
                operate(
                    &mut |_| panic!("spawned"),
                    "get",
                    None,
                    "evil -w",
                    "OPENPROSE_API_KEY"
                )
                .is_err()
            );
        }

        /// A fake `security`: records standard input and replies from files.
        fn fake(dir: &std::path::Path, body: &str) -> PathBuf {
            let program = dir.join("security");
            let script = format!(
                "#!/bin/sh\nR='{}'\n[ \"$1\" = -i ] || exit 9\n/bin/cat > \"$R/stdin\"\n{body}\n",
                dir.display()
            );
            fs::write(&program, script).unwrap();
            fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
            program
        }

        #[test]
        fn macos_store_spawn_sends_the_script_on_stdin_with_an_empty_environment() {
            let dir = TempDir::new().unwrap();
            let program = fake(
                dir.path(),
                "/usr/bin/env > \"$R/env\"; printf 'out'; printf 'err' >&2; exit 3",
            );
            let session = spawn(
                &program,
                "find-generic-password -s x -a api-key\n",
                Duration::from_secs(10),
                &CancellationToken::default(),
            );
            let Session::Finished {
                exit,
                stdout,
                stderr,
            } = session
            else {
                panic!("{session:?}")
            };
            assert_eq!(
                (exit, &stdout[..], &stderr[..]),
                (3, &b"out"[..], &b"err"[..])
            );
            assert_eq!(
                fs::read_to_string(dir.path().join("stdin")).unwrap(),
                "find-generic-password -s x -a api-key\n"
            );
            let environment = fs::read_to_string(dir.path().join("env")).unwrap();
            assert!(
                environment.lines().all(|line| line.starts_with("PWD=")
                    || line.starts_with("SHLVL=")
                    || line.starts_with("_=")
                    || line.starts_with("OLDPWD=")),
                "{environment}"
            );
            // A missing program, a signal and oversized output are failures.
            assert!(matches!(
                spawn(
                    &dir.path().join("missing"),
                    "",
                    Duration::from_secs(1),
                    &CancellationToken::default()
                ),
                Session::Failed
            ));
            let program = fake(dir.path(), "kill -9 $$");
            assert!(matches!(
                spawn(
                    &program,
                    "",
                    Duration::from_secs(5),
                    &CancellationToken::default()
                ),
                Session::Failed
            ));
            let program = fake(dir.path(), "/usr/bin/head -c 9000 /dev/zero; exit 0");
            assert!(matches!(
                spawn(
                    &program,
                    "",
                    Duration::from_secs(5),
                    &CancellationToken::default()
                ),
                Session::Failed
            ));
        }

        #[test]
        fn macos_store_spawn_is_bounded_and_cancellable() {
            let dir = TempDir::new().unwrap();
            let program = fake(dir.path(), "exec /bin/sleep 30");
            let began = Instant::now();
            assert!(matches!(
                spawn(
                    &program,
                    "",
                    Duration::from_millis(300),
                    &CancellationToken::default()
                ),
                Session::Failed
            ));
            assert!(began.elapsed() < Duration::from_secs(5));
            let cancellation = CancellationToken::default();
            let trigger = cancellation.clone();
            let canceller = std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(200));
                trigger.cancel();
            });
            let began = Instant::now();
            assert!(matches!(
                spawn(&program, "", Duration::from_secs(10), &cancellation),
                Session::Cancelled
            ));
            canceller.join().unwrap();
            assert!(began.elapsed() < Duration::from_secs(3));
        }
    }
}
