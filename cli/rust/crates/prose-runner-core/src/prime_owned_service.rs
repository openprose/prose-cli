use crate::error::{ErrorCode, RunnerError};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs;
use std::fs::OpenOptions;
use std::io::{self, Read as _, Write as _};
#[cfg(unix)]
use std::os::unix::fs::FileTypeExt as _;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const RECOVERY_DIRECTORY_PREFIX: &str = "openprose-prime-";
const RECOVERY_MARKER: &str = ".openprose-prime-cleanup.json";
const MAX_RECOVERY_MARKER_BYTES: u64 = 2_048;
const MAX_RECOVERY_ENTRIES: usize = 4_096;
const MAX_RECOVERY_SCRUB_BYTES: u64 = 64 * 1024 * 1024;
const MAX_RECOVERY_DEPTH: usize = 16;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PrimeRecovery {
    pub handle: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RecoveryMarker {
    schema: String,
    handle: String,
    directory: String,
    socket: String,
    detected_version: String,
}

#[derive(Clone, Copy)]
pub(crate) struct SettlementPolicy {
    pub io_timeout: Duration,
    pub shutdown_deadline: Duration,
    pub poll_interval: Duration,
    pub maximum_frame_bytes: usize,
}

impl Default for SettlementPolicy {
    fn default() -> Self {
        Self {
            io_timeout: Duration::from_secs(5),
            shutdown_deadline: Duration::from_secs(5),
            poll_interval: Duration::from_millis(25),
            maximum_frame_bytes: 65_536,
        }
    }
}

pub(crate) fn settle_prime_owned_service(
    directory: &Path,
    socket_path: &Path,
    detected_version: Option<&str>,
    policy: SettlementPolicy,
) -> Result<(), RunnerError> {
    #[cfg(not(unix))]
    {
        let _ = (directory, socket_path, detected_version, policy);
        Err(cleanup_failure("unsupported-platform"))
    }
    #[cfg(unix)]
    settle_unix(directory, socket_path, detected_version, policy)
}

/// Removes all model-visible and credential-bearing regular files from a
/// failed Prime run. If its exact socket remains, writes a private marker and
/// returns the only public authority that can later settle it.
pub(crate) fn prepare_prime_recovery(
    temporary_root: &Path,
    directory: &Path,
    socket_path: &Path,
    detected_version: Option<&str>,
) -> Result<Option<PrimeRecovery>, RunnerError> {
    #[cfg(not(unix))]
    {
        let _ = (temporary_root, directory, socket_path, detected_version);
        Ok(None)
    }
    #[cfg(unix)]
    {
        let detected_version = detected_version
            .filter(|value| matches!(*value, "0.7.0" | "0.8.1"))
            .ok_or_else(|| cleanup_failure("recovery-version-validation"))?;
        let directory = validate_recovery_directory(temporary_root, directory)?;
        let socket_parent = socket_path
            .parent()
            .and_then(|parent| fs::canonicalize(parent).ok());
        if socket_parent.as_deref() != Some(directory.as_path())
            || socket_path.file_name().and_then(|name| name.to_str()) != Some("prime.sock")
        {
            return Err(cleanup_failure("recovery-socket-placement"));
        }
        let socket_path = directory.join("prime.sock");
        scrub_directory_except_socket(&directory, &socket_path)?;
        let socket_metadata = match fs::symlink_metadata(&socket_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                fs::remove_dir(&directory)
                    .map_err(|_| cleanup_failure("recovery-empty-directory-removal"))?;
                return Ok(None);
            }
            Err(_) => return Err(cleanup_failure("recovery-socket-inspection")),
        };
        if !socket_metadata.file_type().is_socket() {
            return Err(cleanup_failure("recovery-socket-validation"));
        }
        let directory_name = directory
            .file_name()
            .and_then(|name| name.to_str())
            .filter(|name| valid_directory_name(name))
            .ok_or_else(|| cleanup_failure("recovery-directory-name"))?;
        let token = uuid::Uuid::now_v7();
        let handle = format!("prime-v1.{directory_name}.{token}");
        let marker = RecoveryMarker {
            schema: "openprose.prime-cleanup-marker/1".to_owned(),
            handle: handle.clone(),
            directory: directory_name.to_owned(),
            socket: "prime.sock".to_owned(),
            detected_version: detected_version.to_owned(),
        };
        write_recovery_marker(&directory.join(RECOVERY_MARKER), &marker)?;
        Ok(Some(PrimeRecovery { handle }))
    }
}

/// Authenticates an opaque failed-run handle, settles only that exact private
/// Prime listener, and removes the marker, stale socket, and empty directory.
pub(crate) fn recover_prime_owned_service(
    temporary_root: &Path,
    handle: &str,
    policy: SettlementPolicy,
) -> Result<(), RunnerError> {
    #[cfg(not(unix))]
    {
        let _ = (temporary_root, handle, policy);
        Err(RunnerError::config(
            "Prime cleanup handles are supported only on POSIX hosts",
        ))
    }
    #[cfg(unix)]
    recover_prime_owned_service_with_finalizer(temporary_root, handle, policy, |_| Ok(()))
}

#[cfg(unix)]
fn recover_prime_owned_service_with_finalizer(
    temporary_root: &Path,
    handle: &str,
    policy: SettlementPolicy,
    before_directory_removal: impl FnOnce(&Path) -> io::Result<()>,
) -> Result<(), RunnerError> {
    let directory_name = parse_recovery_handle(handle)?;
    let root = fs::canonicalize(temporary_root)
        .map_err(|_| RunnerError::config("Prime cleanup temporary root is unavailable"))?;
    let ticket_path = recovery_ticket_path(&root, handle)?;
    let lexical_directory = temporary_root.join(directory_name);
    let directory_exists = match fs::symlink_metadata(&lexical_directory) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(_) => return Err(RunnerError::config("Prime cleanup handle is not authentic")),
    };
    if !directory_exists {
        let marker = read_recovery_marker(&ticket_path)?;
        authenticate_recovery_marker(&marker, handle, directory_name)?;
        fs::remove_file(&ticket_path).map_err(|_| {
            with_recovery_detail(cleanup_failure("recovery-ticket-removal"), handle)
        })?;
        return Ok(());
    }

    let directory = validate_recovery_directory(&root, &lexical_directory)
        .map_err(|error| with_recovery_detail(error, handle))?;
    let marker_path = directory.join(RECOVERY_MARKER);
    let marker_exists = match fs::symlink_metadata(&marker_path) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(_) => return Err(RunnerError::config("Prime cleanup handle is not authentic")),
    };
    if !marker_exists {
        let marker = read_recovery_marker(&ticket_path)?;
        authenticate_recovery_marker(&marker, handle, directory_name)?;
        scrub_finalizing_directory(&directory)
            .map_err(|error| with_recovery_detail(error, handle))?;
        validate_finalizing_directory(&directory)
            .map_err(|error| with_recovery_detail(error, handle))?;
        fs::remove_dir(&directory).map_err(|_| {
            with_recovery_detail(cleanup_failure("recovery-directory-removal"), handle)
        })?;
        fs::remove_file(&ticket_path).map_err(|_| {
            with_recovery_detail(cleanup_failure("recovery-ticket-removal"), handle)
        })?;
        return Ok(());
    }

    let marker = read_recovery_marker(&marker_path)?;
    authenticate_recovery_marker(&marker, handle, directory_name)?;
    validate_recovery_contents(&directory, true)?;
    let lexical_socket = lexical_directory.join("prime.sock");
    settle_prime_owned_service(
        &lexical_directory,
        &lexical_socket,
        Some(&marker.detected_version),
        policy,
    )
    .map_err(|error| with_recovery_detail(error, handle))?;

    validate_recovery_directory(&root, &directory)?;
    let marker_after = read_recovery_marker(&marker_path)?;
    if marker_after != marker {
        return Err(with_recovery_detail(
            cleanup_failure("recovery-marker-changed"),
            handle,
        ));
    }
    validate_recovery_contents(&directory, true)?;
    remove_recovery_socket(&directory, &marker, handle)?;
    let ticket_exists = match fs::symlink_metadata(&ticket_path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Ok(_) => true,
        Err(_) => {
            return Err(with_recovery_detail(
                cleanup_failure("recovery-ticket-inspection"),
                handle,
            ));
        }
    };
    if ticket_exists {
        if !matches!(read_recovery_marker(&ticket_path), Ok(ticket) if ticket == marker) {
            return Err(with_recovery_detail(
                cleanup_failure("recovery-ticket-collision"),
                handle,
            ));
        }
    } else {
        fs::hard_link(&marker_path, &ticket_path)
            .map_err(|_| with_recovery_detail(cleanup_failure("recovery-ticket-create"), handle))?;
        if !matches!(read_recovery_marker(&ticket_path), Ok(ticket) if ticket == marker) {
            let _ = fs::remove_file(&ticket_path);
            return Err(with_recovery_detail(
                cleanup_failure("recovery-ticket-validation"),
                handle,
            ));
        }
    }
    if fs::remove_file(&marker_path).is_err() {
        let _ = fs::remove_file(&ticket_path);
        return Err(with_recovery_detail(
            cleanup_failure("recovery-marker-removal"),
            handle,
        ));
    }
    if before_directory_removal(&directory).is_err() || fs::remove_dir(&directory).is_err() {
        return Err(with_recovery_detail(
            cleanup_failure("recovery-directory-removal"),
            handle,
        ));
    }
    fs::remove_file(&ticket_path)
        .map_err(|_| with_recovery_detail(cleanup_failure("recovery-ticket-removal"), handle))?;
    Ok(())
}

#[cfg(unix)]
fn authenticate_recovery_marker(
    marker: &RecoveryMarker,
    handle: &str,
    directory_name: &str,
) -> Result<(), RunnerError> {
    if marker.schema != "openprose.prime-cleanup-marker/1"
        || marker.handle != handle
        || marker.directory != directory_name
        || marker.socket != "prime.sock"
        || !matches!(marker.detected_version.as_str(), "0.7.0" | "0.8.1")
    {
        return Err(RunnerError::config("Prime cleanup handle is not authentic"));
    }
    Ok(())
}

#[cfg(unix)]
fn recovery_ticket_path(root: &Path, handle: &str) -> Result<PathBuf, RunnerError> {
    let token = handle
        .rsplit_once('.')
        .map(|(_, token)| token)
        .ok_or_else(|| RunnerError::config("Prime cleanup handle is invalid"))?;
    Ok(root.join(format!(".openprose-prime-cleanup-{token}.json")))
}

#[cfg(unix)]
fn validate_finalizing_directory(directory: &Path) -> Result<(), RunnerError> {
    if fs::read_dir(directory)
        .map_err(|_| cleanup_failure("recovery-directory-read"))?
        .next()
        .is_some()
    {
        return Err(cleanup_failure("recovery-unexpected-finalizing-entry"));
    }
    Ok(())
}

#[cfg(unix)]
fn remove_recovery_socket(
    directory: &Path,
    marker: &RecoveryMarker,
    handle: &str,
) -> Result<(), RunnerError> {
    let socket = directory.join(&marker.socket);
    match fs::symlink_metadata(&socket) {
        Ok(metadata) => {
            if !metadata.file_type().is_socket() {
                return Err(with_recovery_detail(
                    cleanup_failure("recovery-stale-socket-validation"),
                    handle,
                ));
            }
            fs::remove_file(&socket).map_err(|_| {
                with_recovery_detail(cleanup_failure("recovery-stale-socket-removal"), handle)
            })
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(with_recovery_detail(
            cleanup_failure("recovery-stale-socket-inspection"),
            handle,
        )),
    }
}

pub(crate) fn with_recovery_detail(error: RunnerError, handle: &str) -> RunnerError {
    error
        .with_detail("cleanupHandle", handle)
        .with_detail("cleanupArgv", json!(["cli", "cleanup", "prime", handle]))
        .with_detail("sensitiveFilesRemoved", true)
}

#[cfg(unix)]
fn validate_recovery_directory(root: &Path, directory: &Path) -> Result<PathBuf, RunnerError> {
    use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};

    if !directory.is_absolute() {
        return Err(cleanup_failure("recovery-directory-relative"));
    }
    let metadata = fs::symlink_metadata(directory)
        .map_err(|_| cleanup_failure("recovery-directory-inspection"))?;
    if !metadata.file_type().is_dir()
        || metadata.permissions().mode() & 0o777 != 0o700
        || metadata.uid() != rustix::process::geteuid().as_raw()
    {
        return Err(cleanup_failure("recovery-directory-validation"));
    }
    let canonical_root =
        fs::canonicalize(root).map_err(|_| cleanup_failure("recovery-root-inspection"))?;
    let canonical_directory = fs::canonicalize(directory)
        .map_err(|_| cleanup_failure("recovery-directory-inspection"))?;
    if canonical_directory.parent() != Some(canonical_root.as_path())
        || !canonical_directory
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(valid_directory_name)
    {
        return Err(cleanup_failure("recovery-directory-ancestry"));
    }
    Ok(canonical_directory)
}

#[cfg(unix)]
fn valid_directory_name(name: &str) -> bool {
    let Some(suffix) = name.strip_prefix(RECOVERY_DIRECTORY_PREFIX) else {
        return false;
    };
    (6..=64).contains(&suffix.len())
        && suffix
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}

#[cfg(unix)]
fn parse_recovery_handle(handle: &str) -> Result<&str, RunnerError> {
    if handle.len() > 160 || !handle.is_ascii() {
        return Err(RunnerError::config("Prime cleanup handle is invalid"));
    }
    let mut parts = handle.split('.');
    let prefix = parts.next();
    let directory = parts.next();
    let token = parts.next();
    if prefix != Some("prime-v1")
        || parts.next().is_some()
        || !directory.is_some_and(valid_directory_name)
        || token
            .and_then(|value| uuid::Uuid::parse_str(value).ok())
            .is_none()
    {
        return Err(RunnerError::config("Prime cleanup handle is invalid"));
    }
    Ok(directory.expect("validated above"))
}

#[cfg(unix)]
fn scrub_directory_except_socket(directory: &Path, socket: &Path) -> Result<(), RunnerError> {
    let mut budget = ScrubBudget::default();
    scrub_directory_entries(directory, Some(socket), 0, &mut budget)
}

#[cfg(unix)]
fn scrub_finalizing_directory(directory: &Path) -> Result<(), RunnerError> {
    let mut budget = ScrubBudget::default();
    scrub_directory_entries(directory, None, 0, &mut budget)
}

#[cfg(unix)]
fn scrub_directory_entries(
    directory: &Path,
    preserved_socket: Option<&Path>,
    depth: usize,
    budget: &mut ScrubBudget,
) -> Result<(), RunnerError> {
    let entries = fs::read_dir(directory).map_err(|_| cleanup_failure("recovery-scrub-read"))?;
    for entry in entries {
        let entry = entry.map_err(|_| cleanup_failure("recovery-scrub-read"))?;
        let path = entry.path();
        budget.entries = budget.entries.saturating_add(1);
        if budget.entries > MAX_RECOVERY_ENTRIES || depth > MAX_RECOVERY_DEPTH {
            return Err(cleanup_failure("recovery-scrub-entry-limit"));
        }
        if preserved_socket.is_some_and(|socket| path == socket) {
            let metadata = fs::symlink_metadata(&path)
                .map_err(|_| cleanup_failure("recovery-socket-inspection"))?;
            if metadata.file_type().is_socket() {
                continue;
            }
        }
        remove_tree_no_follow(&path, depth, budget)?;
    }
    Ok(())
}

#[cfg(unix)]
#[derive(Default)]
struct ScrubBudget {
    entries: usize,
    bytes: u64,
}

#[cfg(unix)]
fn remove_tree_no_follow(
    path: &Path,
    depth: usize,
    budget: &mut ScrubBudget,
) -> Result<(), RunnerError> {
    let metadata =
        fs::symlink_metadata(path).map_err(|_| cleanup_failure("recovery-scrub-inspection"))?;
    if metadata.file_type().is_dir() {
        scrub_directory_entries(path, None, depth + 1, budget)?;
        fs::remove_dir(path).map_err(|_| cleanup_failure("recovery-scrub-directory"))
    } else {
        budget.bytes = budget.bytes.saturating_add(metadata.len());
        if budget.bytes > MAX_RECOVERY_SCRUB_BYTES {
            return Err(cleanup_failure("recovery-scrub-byte-limit"));
        }
        fs::remove_file(path).map_err(|_| cleanup_failure("recovery-scrub-file"))
    }
}

#[cfg(unix)]
fn write_recovery_marker(path: &Path, marker: &RecoveryMarker) -> Result<(), RunnerError> {
    use std::os::unix::fs::OpenOptionsExt as _;
    let bytes =
        serde_json::to_vec(marker).map_err(|_| cleanup_failure("recovery-marker-encode"))?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|_| cleanup_failure("recovery-marker-create"))?;
    file.write_all(&bytes)
        .and_then(|()| file.sync_all())
        .map_err(|_| cleanup_failure("recovery-marker-write"))
}

#[cfg(unix)]
fn read_recovery_marker(path: &Path) -> Result<RecoveryMarker, RunnerError> {
    use std::os::unix::fs::{MetadataExt as _, PermissionsExt as _};
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| RunnerError::config("Prime cleanup handle is not authentic"))?;
    if !metadata.file_type().is_file()
        || metadata.permissions().mode() & 0o777 != 0o600
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.len() > MAX_RECOVERY_MARKER_BYTES
    {
        return Err(RunnerError::config("Prime cleanup handle is not authentic"));
    }
    let bytes =
        fs::read(path).map_err(|_| RunnerError::config("Prime cleanup handle is not authentic"))?;
    serde_json::from_slice(&bytes)
        .map_err(|_| RunnerError::config("Prime cleanup handle is not authentic"))
}

#[cfg(unix)]
fn validate_recovery_contents(directory: &Path, require_marker: bool) -> Result<(), RunnerError> {
    let mut marker_seen = false;
    for entry in fs::read_dir(directory).map_err(|_| cleanup_failure("recovery-directory-read"))? {
        let entry = entry.map_err(|_| cleanup_failure("recovery-directory-read"))?;
        let name = entry.file_name();
        if name == RECOVERY_MARKER {
            marker_seen = true;
            continue;
        }
        if name == "prime.sock" {
            let metadata = fs::symlink_metadata(entry.path())
                .map_err(|_| cleanup_failure("recovery-socket-inspection"))?;
            if metadata.file_type().is_socket() {
                continue;
            }
        }
        return Err(cleanup_failure("recovery-unexpected-entry"));
    }
    if require_marker && !marker_seen {
        return Err(RunnerError::config("Prime cleanup handle is not authentic"));
    }
    Ok(())
}

#[cfg(unix)]
fn settle_unix(
    directory: &Path,
    socket_path: &Path,
    detected_version: Option<&str>,
    policy: SettlementPolicy,
) -> Result<(), RunnerError> {
    use std::os::unix::fs::{FileTypeExt as _, PermissionsExt as _};

    if !directory.is_absolute()
        || !socket_path.is_absolute()
        || socket_path.parent() != Some(directory)
    {
        return Err(cleanup_failure("owned-path-validation"));
    }
    let directory_metadata = fs::symlink_metadata(directory)
        .map_err(|_| cleanup_failure("owned-directory-inspection"))?;
    if !directory_metadata.file_type().is_dir()
        || directory_metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(cleanup_failure("owned-directory-validation"));
    }
    let socket_metadata = match fs::symlink_metadata(socket_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err(cleanup_failure("owned-socket-inspection")),
    };
    if !socket_metadata.file_type().is_socket() {
        return Err(cleanup_failure("owned-socket-validation"));
    }

    let mut stream = match connect_unix_bounded(socket_path, policy.io_timeout) {
        BoundedConnect::Connected(stream) => stream,
        BoundedConnect::Gone => return Ok(()),
        BoundedConnect::TimedOut => return Err(cleanup_failure("daemon-connect-timeout")),
        BoundedConnect::Failed => return Err(cleanup_failure("daemon-connect")),
    };
    stream
        .set_read_timeout(Some(policy.io_timeout))
        .map_err(|_| cleanup_failure("daemon-io-policy"))?;
    stream
        .set_write_timeout(Some(policy.io_timeout))
        .map_err(|_| cleanup_failure("daemon-io-policy"))?;
    let hello = read_json_line(&mut stream, policy.maximum_frame_bytes, "daemon-hello")?;
    if !expected_hello(&hello, socket_path, detected_version) {
        return Err(cleanup_failure("daemon-hello-validation"));
    }
    let id = format!("openprose-shutdown-{}", uuid::Uuid::now_v7());
    let envelope = json!({
        "type":"command",
        "id":id,
        "protocol":{"name":"prime-agent.daemon","version":7},
        "clientId":"openprose-wrapper",
        "command":{"id":id,"type":"shutdown","force":true}
    });
    let mut wire = serde_json::to_vec(&envelope).map_err(|_| cleanup_failure("shutdown-encode"))?;
    wire.push(b'\n');
    stream
        .write_all(&wire)
        .map_err(|_| cleanup_failure("shutdown-write"))?;
    stream
        .flush()
        .map_err(|_| cleanup_failure("shutdown-write"))?;
    let response = read_json_line(&mut stream, policy.maximum_frame_bytes, "shutdown-response")?;
    if !successful_shutdown(&response, &id) {
        return Err(cleanup_failure("shutdown-response-validation"));
    }
    drop(stream);

    let deadline = Instant::now() + policy.shutdown_deadline;
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match connect_unix_bounded(socket_path, policy.io_timeout.min(remaining)) {
            BoundedConnect::Connected(probe) => drop(probe),
            BoundedConnect::Gone => return Ok(()),
            BoundedConnect::TimedOut | BoundedConnect::Failed => {}
        }
        std::thread::sleep(
            policy
                .poll_interval
                .min(deadline.saturating_duration_since(Instant::now())),
        );
    }
    Err(cleanup_failure("shutdown-listener-timeout"))
}

#[cfg(unix)]
enum BoundedConnect {
    Connected(std::os::unix::net::UnixStream),
    Gone,
    TimedOut,
    Failed,
}

/// Opens a Unix socket without ever entering the kernel's unbounded blocking
/// connect path. This is used for the initial shutdown exchange and every
/// listener probe, including a saturated accept backlog.
#[cfg(unix)]
fn connect_unix_bounded(socket_path: &Path, timeout: Duration) -> BoundedConnect {
    use rustix::event::{PollFd, PollFlags, Timespec, poll};
    use rustix::io::{Errno, FdFlags, fcntl_setfd};
    use rustix::net::sockopt::socket_error;
    use rustix::net::{AddressFamily, SocketAddrUnix, SocketType, connect, socket};

    let Ok(address) = SocketAddrUnix::new(socket_path) else {
        return BoundedConnect::Failed;
    };
    let Ok(descriptor) = socket(AddressFamily::UNIX, SocketType::STREAM, None) else {
        return BoundedConnect::Failed;
    };
    if fcntl_setfd(&descriptor, FdFlags::CLOEXEC).is_err() {
        return BoundedConnect::Failed;
    }
    let stream = std::os::unix::net::UnixStream::from(descriptor);
    if stream.set_nonblocking(true).is_err() {
        return BoundedConnect::Failed;
    }
    match connect(&stream, &address) {
        Ok(()) => {}
        Err(error) if error == Errno::NOENT => return classify_connect_disappearance(socket_path),
        Err(error) if error == Errno::CONNREFUSED => return BoundedConnect::Failed,
        Err(error) if error == Errno::INPROGRESS || error == Errno::AGAIN => {
            let deadline = Instant::now() + timeout;
            loop {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return BoundedConnect::TimedOut;
                }
                let timeout = Timespec {
                    tv_sec: i64::try_from(remaining.as_secs()).unwrap_or(i64::MAX),
                    tv_nsec: i64::from(remaining.subsec_nanos()),
                };
                let mut poll_descriptors = [PollFd::new(&stream, PollFlags::OUT)];
                match poll(&mut poll_descriptors, Some(&timeout)) {
                    Ok(0) => return BoundedConnect::TimedOut,
                    Ok(_) => match socket_error(&stream) {
                        Ok(Ok(())) => break,
                        Ok(Err(error)) if error == Errno::NOENT => {
                            return classify_connect_disappearance(socket_path);
                        }
                        Ok(Err(error)) if error == Errno::CONNREFUSED => {
                            return BoundedConnect::Failed;
                        }
                        _ => return BoundedConnect::Failed,
                    },
                    Err(error) if error == Errno::INTR => {}
                    Err(_) => return BoundedConnect::Failed,
                }
            }
        }
        Err(_) => return BoundedConnect::Failed,
    }
    if stream.set_nonblocking(false).is_err() {
        return BoundedConnect::Failed;
    }
    BoundedConnect::Connected(stream)
}

#[cfg(unix)]
fn classify_connect_disappearance(socket_path: &Path) -> BoundedConnect {
    match fs::symlink_metadata(socket_path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => BoundedConnect::Gone,
        _ => BoundedConnect::Failed,
    }
}

#[cfg(unix)]
fn read_json_line(
    stream: &mut std::os::unix::net::UnixStream,
    maximum_frame_bytes: usize,
    phase: &'static str,
) -> Result<Value, RunnerError> {
    let mut bytes = Vec::with_capacity(maximum_frame_bytes.min(4_096));
    let mut byte = [0_u8; 1];
    loop {
        match stream.read(&mut byte) {
            Ok(0) => {
                return Err(cleanup_failure(match phase {
                    "daemon-hello" => "daemon-hello-closed",
                    _ => "shutdown-response-closed",
                }));
            }
            Ok(_) if byte[0] == b'\n' => break,
            Ok(_) => {
                if bytes.len() == maximum_frame_bytes {
                    return Err(cleanup_failure(match phase {
                        "daemon-hello" => "daemon-hello-limit",
                        _ => "shutdown-response-limit",
                    }));
                }
                bytes.push(byte[0]);
            }
            Err(_) => {
                return Err(cleanup_failure(match phase {
                    "daemon-hello" => "daemon-hello-timeout",
                    _ => "shutdown-response-timeout",
                }));
            }
        }
    }
    serde_json::from_slice(&bytes).map_err(|_| {
        cleanup_failure(match phase {
            "daemon-hello" => "daemon-hello-malformed",
            _ => "shutdown-response-malformed",
        })
    })
}

#[cfg(unix)]
fn expected_hello(value: &Value, socket_path: &Path, detected_version: Option<&str>) -> bool {
    value.get("type").and_then(Value::as_str) == Some("daemon_hello")
        && value.get("socketPath").and_then(Value::as_str) == socket_path.to_str()
        && value
            .get("protocol")
            .and_then(|protocol| protocol.get("name"))
            .and_then(Value::as_str)
            == Some("prime-agent.daemon")
        && value
            .get("protocol")
            .and_then(|protocol| protocol.get("version"))
            .and_then(Value::as_u64)
            == Some(7)
        && detected_version
            .is_none_or(|version| value.get("appVersion").and_then(Value::as_str) == Some(version))
}

#[cfg(unix)]
fn successful_shutdown(value: &Value, id: &str) -> bool {
    value.get("type").and_then(Value::as_str) == Some("response")
        && value.get("id").and_then(Value::as_str) == Some(id)
        && value.get("command").and_then(Value::as_str) == Some("shutdown")
        && value.get("success").and_then(Value::as_bool) == Some(true)
}

fn cleanup_failure(_phase: &'static str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ProcessCleanupFailed)
        .with_detail("phase", "owned-service-settlement")
        .with_detail("processResource", "owned-prime-harness-service")
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use std::fs;
    use std::io::{BufRead as _, BufReader};
    use std::os::unix::fs::PermissionsExt as _;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::PathBuf;
    use std::sync::mpsc::{self, Receiver};
    use std::thread;

    const POLICY: SettlementPolicy = SettlementPolicy {
        io_timeout: Duration::from_millis(80),
        shutdown_deadline: Duration::from_millis(160),
        poll_interval: Duration::from_millis(10),
        maximum_frame_bytes: 4_096,
    };

    struct FixedClock;
    impl crate::Clock for FixedClock {
        fn now_rfc3339(&self) -> String {
            "2026-01-02T03:04:05.000Z".to_owned()
        }
    }

    struct FixedIds;
    impl crate::IdSource for FixedIds {
        fn next_invocation_id(&self) -> String {
            "cleanup-operation".to_owned()
        }
    }

    struct OwnedPaths {
        root: tempfile::TempDir,
        directory: PathBuf,
        socket: PathBuf,
    }

    fn owned_paths() -> OwnedPaths {
        let root = tempfile::tempdir().unwrap();
        let directory = root.path().join("owned");
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = directory.join("prime.sock");
        OwnedPaths {
            root,
            directory,
            socket,
        }
    }

    fn hello(socket: &Path) -> Value {
        json!({
            "type":"daemon_hello",
            "socketPath":socket,
            "protocol":{"name":"prime-agent.daemon","version":7},
            "appVersion":"0.7.0",
            "clientId":"fixture-daemon",
            "serverCapabilities":[]
        })
    }

    fn spawn_server(
        socket: &Path,
        greeting: String,
        response: impl FnOnce(&Value) -> Value + Send + 'static,
        keep_listening: bool,
    ) -> (Receiver<Value>, thread::JoinHandle<()>) {
        let listener = UnixListener::bind(socket).unwrap();
        let socket = socket.to_owned();
        let (sender, receiver) = mpsc::channel();
        let handle = thread::spawn(move || {
            let mut listener = Some(listener);
            let (mut stream, _) = listener.as_ref().unwrap().accept().unwrap();
            if writeln!(stream, "{greeting}").is_err() {
                return;
            }
            let mut command = String::new();
            if BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut command)
                .unwrap()
                > 0
            {
                let command: Value = serde_json::from_str(command.trim_end()).unwrap();
                sender.send(command.clone()).unwrap();
                if !keep_listening {
                    drop(listener.take());
                    fs::remove_file(&socket).unwrap();
                }
                writeln!(stream, "{}", response(&command)).unwrap();
            }
            drop(stream);
            if keep_listening {
                thread::sleep(Duration::from_millis(400));
            }
            if listener.take().is_some() {
                fs::remove_file(socket).unwrap();
            }
        });
        (receiver, handle)
    }

    fn close_unused_server(
        socket: &Path,
        commands: &Receiver<Value>,
        server: thread::JoinHandle<()>,
    ) {
        assert!(commands.try_recv().is_err());
        let stream = UnixStream::connect(socket).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        // Send EOF without discarding the greeting. Abruptly dropping an unread
        // socket can reset the server's read even when no command was sent.
        stream.shutdown(std::net::Shutdown::Write).unwrap();
        let mut greeting = String::new();
        BufReader::new(stream).read_line(&mut greeting).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&greeting).unwrap(),
            hello(socket)
        );
        server.join().unwrap();
        assert!(commands.try_recv().is_err());
    }

    fn successful_response(command: &Value) -> Value {
        json!({"type":"response","id":command["id"],"command":"shutdown","success":true})
    }

    #[test]
    fn absent_socket_is_an_accepted_provider_free_fake() {
        let paths = owned_paths();
        settle_prime_owned_service(&paths.directory, &paths.socket, Some("0.7.0"), POLICY).unwrap();
    }

    #[test]
    fn exact_hello_and_shutdown_envelope_settle_the_listener() {
        let paths = owned_paths();
        let (commands, handle) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );
        settle_prime_owned_service(&paths.directory, &paths.socket, Some("0.7.0"), POLICY).unwrap();
        let command = commands.recv().unwrap();
        assert_eq!(command["type"], "command");
        assert_eq!(
            command["protocol"],
            json!({"name":"prime-agent.daemon","version":7})
        );
        assert_eq!(command["clientId"], "openprose-wrapper");
        assert_eq!(
            command["command"],
            json!({"id":command["id"],"type":"shutdown","force":true})
        );
        assert!(command["id"].as_str().unwrap().len() <= 96);
        handle.join().unwrap();
    }

    #[test]
    fn admitted_prime_0_8_1_exactly_matches_daemon_app_version() {
        let paths = owned_paths();
        let mut greeting = hello(&paths.socket);
        greeting["appVersion"] = json!("0.8.1");
        let (_commands, handle) = spawn_server(
            &paths.socket,
            greeting.to_string(),
            successful_response,
            false,
        );
        settle_prime_owned_service(&paths.directory, &paths.socket, Some("0.8.1"), POLICY).unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn malformed_hello_is_rejected_without_a_command() {
        let paths = owned_paths();
        let (_commands, handle) = spawn_server(
            &paths.socket,
            "not-json".to_owned(),
            successful_response,
            false,
        );
        assert_eq!(
            settle_prime_owned_service(&paths.directory, &paths.socket, None, POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
        UnixStream::connect(&paths.socket).ok();
        handle.join().unwrap();
    }

    #[test]
    fn wrong_protocol_path_and_detected_app_version_are_rejected() {
        for invalid in ["protocol", "path", "version"] {
            let paths = owned_paths();
            let mut greeting = hello(&paths.socket);
            match invalid {
                "protocol" => greeting["protocol"]["version"] = json!(6),
                "path" => greeting["socketPath"] = json!("/tmp/not-owned.sock"),
                "version" => greeting["appVersion"] = json!("9.9.9"),
                _ => unreachable!(),
            }
            let (_commands, handle) = spawn_server(
                &paths.socket,
                greeting.to_string(),
                successful_response,
                false,
            );
            assert_eq!(
                settle_prime_owned_service(&paths.directory, &paths.socket, Some("0.7.0"), POLICY,)
                    .unwrap_err()
                    .code,
                ErrorCode::ProcessCleanupFailed,
                "{invalid}"
            );
            UnixStream::connect(&paths.socket).ok();
            handle.join().unwrap();
        }
    }

    #[test]
    fn bad_response_is_rejected() {
        let paths = owned_paths();
        let (_commands, handle) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            |_| json!({"type":"response","id":"wrong","command":"shutdown","success":true}),
            false,
        );
        assert_eq!(
            settle_prime_owned_service(&paths.directory, &paths.socket, None, POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
        handle.join().unwrap();
    }

    #[test]
    fn silent_hello_and_still_listening_are_bounded_failures() {
        let silent = owned_paths();
        let silent_listener = UnixListener::bind(&silent.socket).unwrap();
        let silent_handle = thread::spawn(move || {
            let (_stream, _) = silent_listener.accept().unwrap();
            thread::sleep(Duration::from_millis(200));
        });
        let started = std::time::Instant::now();
        assert_eq!(
            settle_prime_owned_service(&silent.directory, &silent.socket, None, POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
        assert!(started.elapsed() < Duration::from_millis(800));
        silent_handle.join().unwrap();

        let paths = owned_paths();
        let (_commands, handle) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            true,
        );
        assert_eq!(
            settle_prime_owned_service(&paths.directory, &paths.socket, None, POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
        handle.join().unwrap();
    }

    #[test]
    fn saturated_listener_backlog_cannot_block_a_connect() {
        use rustix::net::{AddressFamily, SocketAddrUnix, SocketType, bind, listen, socket};

        let paths = owned_paths();
        let listener = socket(AddressFamily::UNIX, SocketType::STREAM, None).unwrap();
        bind(&listener, &SocketAddrUnix::new(&paths.socket).unwrap()).unwrap();
        listen(&listener, 0).unwrap();
        let started = Instant::now();
        let mut queued = Vec::new();
        for _ in 0..512 {
            match connect_unix_bounded(&paths.socket, Duration::from_millis(5)) {
                BoundedConnect::Connected(stream) => queued.push(stream),
                BoundedConnect::Gone | BoundedConnect::TimedOut | BoundedConnect::Failed => break,
            }
        }
        // Kernels differ in whether backlog pressure appears as EAGAIN,
        // ECONNREFUSED, or continued local admission. The invariant is that
        // every classification returns within its explicit deadline.
        assert!(started.elapsed() < Duration::from_secs(2));
        let settlement_started = Instant::now();
        assert_eq!(
            settle_prime_owned_service(&paths.directory, &paths.socket, Some("0.7.0"), POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
        assert!(settlement_started.elapsed() < Duration::from_millis(800));
        assert!(paths.directory.exists());
        assert!(paths.socket.exists());
    }

    #[test]
    fn refused_existing_socket_is_not_mistaken_for_listener_disappearance() {
        let paths = owned_paths();
        let listener = UnixListener::bind(&paths.socket).unwrap();
        drop(listener);
        assert!(paths.socket.exists());
        assert_eq!(
            settle_prime_owned_service(&paths.directory, &paths.socket, Some("0.7.0"), POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
        assert!(paths.directory.exists());
        assert!(paths.socket.exists());
    }

    #[test]
    fn regular_file_is_not_treated_as_an_owned_service() {
        let paths = owned_paths();
        fs::write(&paths.socket, b"not a socket").unwrap();
        assert_eq!(
            settle_prime_owned_service(&paths.directory, &paths.socket, None, POLICY)
                .unwrap_err()
                .code,
            ErrorCode::ProcessCleanupFailed
        );
    }

    fn recovery_paths() -> OwnedPaths {
        let root = tempfile::tempdir_in("/tmp").unwrap();
        let directory = root.path().join("openprose-prime-Fixture1");
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = directory.join("prime.sock");
        OwnedPaths {
            root,
            directory,
            socket,
        }
    }

    #[test]
    fn recovery_scrubs_sensitive_files_then_settles_only_the_marked_service() {
        let paths = recovery_paths();
        fs::write(paths.directory.join("language-image.bin"), b"image-secret").unwrap();
        fs::write(paths.directory.join("task-envelope.json"), b"task-secret").unwrap();
        let config = paths.directory.join("credential-config");
        fs::create_dir(&config).unwrap();
        let nested = config.join("cache");
        fs::create_dir(&nested).unwrap();
        fs::write(nested.join("credentials.json"), b"credential-secret").unwrap();
        let outside = paths.root.path().join("outside-secret");
        fs::write(&outside, b"must-survive").unwrap();
        std::os::unix::fs::symlink(&outside, config.join("outside-link")).unwrap();
        let (_commands, server) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );

        let recovery = prepare_prime_recovery(
            paths.root.path(),
            &paths.directory,
            &paths.socket,
            Some("0.7.0"),
        )
        .unwrap()
        .unwrap();
        assert!(
            recovery
                .handle
                .starts_with("prime-v1.openprose-prime-Fixture1.")
        );
        let retained = fs::read_dir(&paths.directory)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect::<Vec<_>>();
        assert_eq!(retained.len(), 2);
        assert!(retained.iter().any(|name| name == "prime.sock"));
        assert!(retained.iter().any(|name| name == RECOVERY_MARKER));
        assert!(!paths.directory.join("language-image.bin").exists());
        assert!(!paths.directory.join("task-envelope.json").exists());
        assert!(!config.exists());
        assert_eq!(fs::read(&outside).unwrap(), b"must-survive");

        let outcome = crate::runner::execute_prime_cleanup(
            &recovery.handle,
            crate::OutputMode::Json,
            &FixedClock,
            &FixedIds,
            paths.root.path(),
        );
        assert_eq!(outcome.exit_code, 0);
        let crate::output::Payload::Json(report) = outcome.payload else {
            panic!("cleanup machine mode must emit one JSON document");
        };
        assert_eq!(report["schema"], "openprose.prime-cleanup/1");
        assert_eq!(report["cleanupHandle"], recovery.handle);
        assert_eq!(report["serviceSettlement"], "verified");
        assert!(
            !report
                .to_string()
                .contains(paths.root.path().to_str().unwrap())
        );
        server.join().unwrap();
        assert!(!paths.directory.exists());
    }

    #[test]
    fn forged_handle_and_tampered_marker_never_contact_or_remove_a_sibling() {
        let paths = recovery_paths();
        let sibling = paths.root.path().join("sibling");
        fs::write(&sibling, b"keep").unwrap();
        let error = recover_prime_owned_service(
            paths.root.path(),
            "prime-v1.openprose-prime-Fixture1.not-a-uuid",
            POLICY,
        )
        .unwrap_err();
        assert_eq!(error.code, ErrorCode::ConfigInvalid);
        assert_eq!(fs::read(&sibling).unwrap(), b"keep");

        let (commands, server) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );
        let recovery = prepare_prime_recovery(
            paths.root.path(),
            &paths.directory,
            &paths.socket,
            Some("0.7.0"),
        )
        .unwrap()
        .unwrap();
        let marker_path = paths.directory.join(RECOVERY_MARKER);
        let mut marker: Value = serde_json::from_slice(&fs::read(&marker_path).unwrap()).unwrap();
        marker["handle"] =
            json!("prime-v1.openprose-prime-other.00000000-0000-7000-8000-000000000000");
        fs::write(&marker_path, serde_json::to_vec(&marker).unwrap()).unwrap();
        let error =
            recover_prime_owned_service(paths.root.path(), &recovery.handle, POLICY).unwrap_err();
        assert_eq!(error.code, ErrorCode::ConfigInvalid);
        assert_eq!(fs::read(&sibling).unwrap(), b"keep");
        close_unused_server(&paths.socket, &commands, server);
    }

    #[test]
    fn unexpected_post_marker_entry_blocks_recovery_without_removing_it() {
        let paths = recovery_paths();
        let (commands, server) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );
        let recovery = prepare_prime_recovery(
            paths.root.path(),
            &paths.directory,
            &paths.socket,
            Some("0.7.0"),
        )
        .unwrap()
        .unwrap();
        let unexpected = paths.directory.join("concurrent-run");
        fs::write(&unexpected, b"do-not-touch").unwrap();
        let error =
            recover_prime_owned_service(paths.root.path(), &recovery.handle, POLICY).unwrap_err();
        assert_eq!(error.code, ErrorCode::ProcessCleanupFailed);
        assert_eq!(fs::read(&unexpected).unwrap(), b"do-not-touch");
        close_unused_server(&paths.socket, &commands, server);
    }

    #[test]
    fn final_directory_removal_failure_keeps_an_authentic_retry_ticket() {
        let paths = recovery_paths();
        let outside = paths.root.path().join("outside-late-entry-target");
        fs::write(&outside, b"must-survive").unwrap();
        let (_commands, server) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );
        let recovery = prepare_prime_recovery(
            paths.root.path(),
            &paths.directory,
            &paths.socket,
            Some("0.7.0"),
        )
        .unwrap()
        .unwrap();
        let error = recover_prime_owned_service_with_finalizer(
            paths.root.path(),
            &recovery.handle,
            POLICY,
            |directory| {
                fs::write(directory.join("injected-finalizer-blocker"), b"retry")?;
                std::os::unix::fs::symlink(&outside, directory.join("injected-finalizer-symlink"))
            },
        )
        .unwrap_err();
        assert_eq!(error.code, ErrorCode::ProcessCleanupFailed);
        let details = error.details.as_deref().unwrap();
        assert_eq!(details["cleanupHandle"], recovery.handle);
        assert_eq!(
            details["cleanupArgv"],
            json!(["cli", "cleanup", "prime", recovery.handle])
        );
        assert!(!paths.directory.join(RECOVERY_MARKER).exists());
        assert!(
            recovery_ticket_path(paths.root.path(), &recovery.handle)
                .unwrap()
                .exists()
        );
        server.join().unwrap();

        recover_prime_owned_service(paths.root.path(), &recovery.handle, POLICY).unwrap();
        assert!(!paths.directory.exists());
        assert_eq!(fs::read(&outside).unwrap(), b"must-survive");
        assert!(
            !recovery_ticket_path(paths.root.path(), &recovery.handle)
                .unwrap()
                .exists()
        );
    }

    #[test]
    fn recovery_scrub_applies_the_entry_budget_while_streaming_and_never_follows_links() {
        let paths = recovery_paths();
        let outside = paths.root.path().join("outside-entry-limit-target");
        fs::write(&outside, b"must-survive").unwrap();
        std::os::unix::fs::symlink(&outside, paths.directory.join("outside-link")).unwrap();
        for index in 0..MAX_RECOVERY_ENTRIES {
            fs::File::create(paths.directory.join(format!("entry-{index:04}"))).unwrap();
        }

        let started = Instant::now();
        let error = scrub_directory_except_socket(&paths.directory, &paths.socket).unwrap_err();
        assert_eq!(error.code, ErrorCode::ProcessCleanupFailed);
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(fs::read(&outside).unwrap(), b"must-survive");
        assert!(paths.directory.exists());
    }

    #[test]
    fn retry_resumes_if_interrupted_after_ticket_creation() {
        let paths = recovery_paths();
        let (_commands, server) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );
        let recovery = prepare_prime_recovery(
            paths.root.path(),
            &paths.directory,
            &paths.socket,
            Some("0.7.0"),
        )
        .unwrap()
        .unwrap();
        fs::hard_link(
            paths.directory.join(RECOVERY_MARKER),
            recovery_ticket_path(paths.root.path(), &recovery.handle).unwrap(),
        )
        .unwrap();

        recover_prime_owned_service(paths.root.path(), &recovery.handle, POLICY).unwrap();
        server.join().unwrap();
        assert!(!paths.directory.exists());
        assert!(
            !recovery_ticket_path(paths.root.path(), &recovery.handle)
                .unwrap()
                .exists()
        );
    }

    #[test]
    fn changed_root_and_symlinked_directory_fail_without_contacting_service() {
        let paths = recovery_paths();
        let (commands, server) = spawn_server(
            &paths.socket,
            hello(&paths.socket).to_string(),
            successful_response,
            false,
        );
        let recovery = prepare_prime_recovery(
            paths.root.path(),
            &paths.directory,
            &paths.socket,
            Some("0.7.0"),
        )
        .unwrap()
        .unwrap();
        let other_root = tempfile::tempdir_in("/tmp").unwrap();
        let directory_name = parse_recovery_handle(&recovery.handle).unwrap();
        std::os::unix::fs::symlink(&paths.directory, other_root.path().join(directory_name))
            .unwrap();
        let error =
            recover_prime_owned_service(other_root.path(), &recovery.handle, POLICY).unwrap_err();
        assert!(matches!(
            error.code,
            ErrorCode::ConfigInvalid | ErrorCode::ProcessCleanupFailed
        ));
        assert!(commands.try_recv().is_err());
        assert!(paths.directory.exists());
        close_unused_server(&paths.socket, &commands, server);
    }
}
