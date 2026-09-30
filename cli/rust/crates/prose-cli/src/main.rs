mod weave_host;
use prose_runner_core::error::{ErrorCode, RunnerError};
use prose_runner_core::image::RuntimeImage;
use prose_runner_core::invocation::{Action, ParsedInvocation, RunnerCommand};
use prose_runner_core::output::{CommandOutcome, error_outcome};
use prose_runner_core::runner::{
    HELP, RUNNER_VERSION, execute_prime_cleanup, execute_with_cancellation_and_human_stream,
};
use prose_runner_core::service::ServiceCommand;
use prose_runner_core::{
    CancellationToken, OutputMode, SignalCancellationGuard, SystemClock, SystemContext,
    SystemIdSource, parse_invocation, resolve_config,
};
use std::collections::BTreeSet;
use std::io;
use std::path::{Component, Path};
use std::process::ExitCode;

const IMAGE_BUNDLE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/current.bundle.bin"));
const BUNDLE_MAGIC: &[u8] = b"OPENPROSE-IMAGE-BUNDLE\0\x01";
const MAX_MANIFEST_BYTES: usize = 1024 * 1024;
const MAX_ENTRY_BYTES: usize = 16 * 1024 * 1024;
const MAX_IMAGE_BYTES: usize = 64 * 1024 * 1024;
const MAX_FILES: usize = 4096;
const MAX_PATH_BYTES: usize = 4096;

#[derive(Debug)]
struct EmbeddedBundle<'a> {
    manifest: &'a [u8],
    files: Vec<(&'a str, &'a [u8])>,
}

#[derive(Debug)]
struct BundleReader<'a> {
    bytes: &'a [u8],
    offset: usize,
}

impl<'a> BundleReader<'a> {
    fn take(&mut self, length: usize, field: &str) -> Result<&'a [u8], RunnerError> {
        let end = self
            .offset
            .checked_add(length)
            .ok_or_else(|| image_too_large("embedded bundle offset overflow"))?;
        let value = self
            .bytes
            .get(self.offset..end)
            .ok_or_else(|| image_invalid(format!("embedded bundle is truncated at {field}")))?;
        self.offset = end;
        Ok(value)
    }

    fn u32(&mut self, field: &str) -> Result<usize, RunnerError> {
        let bytes: [u8; 4] = self
            .take(4, field)?
            .try_into()
            .expect("four bytes requested");
        Ok(u32::from_be_bytes(bytes) as usize)
    }

    fn u64(&mut self, field: &str) -> Result<usize, RunnerError> {
        let bytes: [u8; 8] = self
            .take(8, field)?
            .try_into()
            .expect("eight bytes requested");
        usize::try_from(u64::from_be_bytes(bytes))
            .map_err(|_| image_too_large("embedded bundle entry length exceeds this platform"))
    }
}

fn embedded_runtime_image() -> Result<RuntimeImage, RunnerError> {
    let bundle = parse_embedded_bundle(IMAGE_BUNDLE)?;
    let image = RuntimeImage::from_embedded(bundle.manifest, &bundle.files)?;
    let expected = image
        .manifest
        .payload
        .iter()
        .map(|entry| entry.path.as_str())
        .chain([
            image.manifest.task_envelope.path.as_str(),
            image.manifest.one_field_framing.path.as_str(),
            image.manifest.terminal_envelope.path.as_str(),
        ])
        .collect::<Vec<_>>();
    if bundle
        .files
        .iter()
        .map(|(path, _)| *path)
        .ne(expected.iter().copied())
    {
        return Err(image_invalid(
            "embedded bundle file order does not match manifest authority",
        ));
    }
    Ok(image)
}

fn parse_embedded_bundle(bytes: &[u8]) -> Result<EmbeddedBundle<'_>, RunnerError> {
    let maximum = MAX_IMAGE_BYTES
        .checked_add(BUNDLE_MAGIC.len() + 8)
        .and_then(|value| value.checked_add(MAX_FILES * (12 + MAX_PATH_BYTES)))
        .expect("bundle limit constants fit usize");
    if bytes.len() > maximum {
        return Err(image_too_large("embedded bundle exceeds aggregate limit"));
    }
    let mut reader = BundleReader { bytes, offset: 0 };
    if reader.take(BUNDLE_MAGIC.len(), "magic")? != BUNDLE_MAGIC {
        return Err(image_invalid(
            "embedded bundle magic/version is unsupported",
        ));
    }
    let manifest_length = reader.u32("manifest length")?;
    if manifest_length > MAX_MANIFEST_BYTES {
        return Err(image_too_large(
            "embedded bundle manifest exceeds its limit",
        ));
    }
    let manifest = reader.take(manifest_length, "manifest bytes")?;
    validate_bundle_text(manifest, "manifest.json")?;
    let count = reader.u32("file count")?;
    if count > MAX_FILES {
        return Err(image_too_large(
            "embedded bundle file count exceeds its limit",
        ));
    }
    let mut files = Vec::with_capacity(count);
    let mut seen = BTreeSet::new();
    let mut total = manifest.len();
    for index in 0..count {
        let path_length = reader.u32(&format!("file[{index}] path length"))?;
        if path_length == 0 || path_length > MAX_PATH_BYTES {
            return Err(image_invalid("embedded bundle path length is invalid"));
        }
        let path_bytes = reader.take(path_length, &format!("file[{index}] path"))?;
        let path = std::str::from_utf8(path_bytes)
            .map_err(|_| image_invalid("embedded bundle path is not UTF-8"))?;
        validate_bundle_path(path)?;
        if !seen.insert(path) {
            return Err(image_invalid(format!(
                "embedded bundle contains duplicate path {path:?}"
            )));
        }
        let length = reader.u64(&format!("file[{index}] length"))?;
        if length > MAX_ENTRY_BYTES {
            return Err(image_too_large("embedded bundle entry exceeds its limit"));
        }
        let body = reader.take(length, &format!("file[{index}] bytes"))?;
        validate_bundle_text(body, path)?;
        total = total
            .checked_add(body.len())
            .ok_or_else(|| image_too_large("embedded bundle length overflow"))?;
        if total > MAX_IMAGE_BYTES {
            return Err(image_too_large(
                "embedded bundle files exceed aggregate limit",
            ));
        }
        files.push((path, body));
    }
    if reader.offset != bytes.len() {
        return Err(image_invalid("embedded bundle contains trailing bytes"));
    }
    Ok(EmbeddedBundle { manifest, files })
}

fn validate_bundle_path(path: &str) -> Result<(), RunnerError> {
    if !(path.starts_with("payload/") || path.starts_with("contracts/"))
        || path.contains('\\')
        || path.contains('\0')
        || !path
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'/'))
        || path
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(image_invalid(format!(
            "embedded bundle contains unsafe path {path:?}"
        )));
    }
    let parsed = Path::new(path);
    if parsed.is_absolute()
        || parsed
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(image_invalid(format!(
            "embedded bundle contains unsafe path {path:?}"
        )));
    }
    Ok(())
}

fn validate_bundle_text(bytes: &[u8], path: &str) -> Result<(), RunnerError> {
    if bytes.starts_with(&[0xef, 0xbb, 0xbf]) {
        return Err(image_invalid(format!(
            "embedded bundle file {path:?} contains a byte-order mark"
        )));
    }
    if bytes.contains(&b'\0') {
        return Err(image_invalid(format!(
            "embedded bundle file {path:?} contains NUL"
        )));
    }
    if bytes.contains(&b'\r') {
        return Err(image_invalid(format!(
            "embedded bundle file {path:?} does not use LF newlines"
        )));
    }
    std::str::from_utf8(bytes)
        .map_err(|_| image_invalid(format!("embedded bundle file {path:?} is not UTF-8")))?;
    Ok(())
}

fn image_invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::catalog(ErrorCode::ImageInvalid).with_detail("reason", reason.into())
}

fn image_too_large(reason: impl Into<String>) -> RunnerError {
    RunnerError::catalog(ErrorCode::ImageTooLarge).with_detail("reason", reason.into())
}

fn main() -> ExitCode {
    // A dev-endpoint build names itself in copyable commands exactly as it
    // was invoked (argv[0]); without one, by its executable's file name.
    let invoked = std::env::args_os()
        .next()
        .and_then(|name| name.into_string().ok())
        .filter(|name| !name.is_empty())
        .or_else(|| {
            std::env::current_exe().ok().and_then(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .map(str::to_owned)
            })
        });
    prose_runner_core::service::render::record_invoked_name(invoked.as_deref());
    // Arguments that are not UTF-8 never panic: each invalid sequence becomes
    // U+FFFD, exactly as the Bun build decodes its argv.
    let args: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|argument| argument.to_string_lossy().into_owned())
        .collect();
    if let Some((globals, tail)) = prose_runner_core::invocation::weave_route(&args) {
        return ExitCode::from(weave_host::run(globals, tail));
    }
    let cancellation = CancellationToken::default();
    let _signal_guard = match SignalCancellationGuard::install(&cancellation) {
        Ok(guard) => guard,
        Err(error) => {
            eprintln!("INTERNAL_ERROR at runner: cannot install cancellation handlers: {error}");
            return ExitCode::from(70);
        }
    };
    // A closed terminal (SIGHUP) cancels like Ctrl-C (CANCELLED, exit 24),
    // as in the Bun build. Windows Ctrl+Break is handled by neither product.
    #[cfg(unix)]
    let _hangup = match hangup_cancels(&cancellation) {
        Ok(registration) => registration,
        Err(error) => {
            eprintln!("INTERNAL_ERROR at runner: cannot install cancellation handlers: {error}");
            return ExitCode::from(70);
        }
    };
    let mut stdout = io::stdout().lock();
    let outcome = prepare(&args, &cancellation, &mut stdout);
    let mut stderr = io::stderr().lock();
    let mut stdout = ClosedPipeTolerant {
        inner: stdout,
        closed: false,
    };
    if let Err(error) = outcome.render(&mut stdout, &mut stderr) {
        eprintln!("INTERNAL_ERROR at runner: cannot write command output: {error}");
        return ExitCode::from(70);
    }
    ExitCode::from(outcome.exit_code)
}

/// Registers SIGHUP to cancel `cancellation`: the handler only sets a flag,
/// which a watcher thread turns into the cooperative cancellation.
#[cfg(unix)]
fn hangup_cancels(cancellation: &CancellationToken) -> io::Result<signal_hook::SigId> {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};
    let flag = Arc::new(AtomicBool::new(false));
    let registration = signal_hook::flag::register(signal_hook::consts::SIGHUP, Arc::clone(&flag))?;
    let token = cancellation.clone();
    std::thread::spawn(move || {
        while !flag.load(Ordering::Acquire) {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        token.cancel();
    });
    Ok(registration)
}

/// Standard output that treats a reader that went away (EPIPE) as the end of
/// the output: `prose cli service operations | head -1` exits silently with
/// the command's own exit code, as the Bun build does, instead of reporting
/// `INTERNAL_ERROR` with exit 70. Anything later on stderr is
/// still written.
struct ClosedPipeTolerant<W> {
    inner: W,
    closed: bool,
}

impl<W: io::Write> io::Write for ClosedPipeTolerant<W> {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        if self.closed {
            return Ok(buffer.len());
        }
        match self.inner.write(buffer) {
            Err(error) if error.kind() == io::ErrorKind::BrokenPipe => {
                self.closed = true;
                Ok(buffer.len())
            }
            other => other,
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.closed {
            return Ok(());
        }
        match self.inner.flush() {
            Err(error) if error.kind() == io::ErrorKind::BrokenPipe => {
                self.closed = true;
                Ok(())
            }
            other => other,
        }
    }
}

/// Parses the argv. A service argv's invocation error is the service
/// envelope in JSON modes, never a bare runner error, and a rejected service
/// invocation carries the argv so its envelope names the operation the argv
/// names.
fn parse(args: &[String]) -> Result<ParsedInvocation, CommandOutcome> {
    let error_mode = output_hint(args);
    let mut parsed = parse_invocation(args.to_vec()).map_err(|error| {
        let system = SystemContext::capture().ok();
        prose_runner_core::service::argv_error_outcome(args, &error, error_mode, system.as_ref())
            .unwrap_or_else(|| error_outcome(error, error_mode, &SystemClock, &SystemIdSource))
    })?;
    match parsed.action {
        Action::Runner {
            command: RunnerCommand::Service(ServiceCommand::Invalid(ref mut invalid)),
            ..
        } => invalid.argv = args.to_vec(),
        Action::Forward {
            hosted_rejection: Some(ref mut rejection),
            ..
        } => {
            if let ServiceCommand::Invalid(ref mut invalid) = **rejection {
                invalid.argv = args.to_vec();
            }
        }
        _ => {}
    }
    Ok(parsed)
}

fn prepare(
    args: &[String],
    cancellation: &CancellationToken,
    human_stdout: &mut dyn io::Write,
) -> CommandOutcome {
    let clock = SystemClock;
    let ids = SystemIdSource;
    let parsed = match parse(args) {
        Ok(parsed) => parsed,
        Err(outcome) => return outcome,
    };

    // Runner identity operations must remain available even when the current
    // directory or configuration is broken.
    match parsed.action {
        Action::Help => return CommandOutcome::human(HELP, "", 0),
        Action::Version => {
            return CommandOutcome::human(format!("prose {RUNNER_VERSION} (rust)\n"), "", 0);
        }
        Action::Runner {
            command: RunnerCommand::CleanupPrime(ref handle),
            json,
        } => {
            let mode = if json {
                OutputMode::Json
            } else {
                parsed.globals.output.unwrap_or_default()
            };
            return execute_prime_cleanup(handle, mode, &clock, &ids, &std::env::temp_dir());
        }
        Action::Runner {
            command: RunnerCommand::Service(ServiceCommand::Help(ref text)),
            ..
        } => {
            // In a JSON mode (`prose --output json cli run --help`) help is
            // the envelope with the text and the command records.
            return prose_runner_core::service::help_outcome(
                text,
                parsed.globals.output.unwrap_or_default(),
            );
        }
        _ => {}
    }

    let system = match SystemContext::capture() {
        Ok(system) => system,
        Err(error) => {
            return error_outcome(
                error,
                parsed.globals.output.unwrap_or_default(),
                &clock,
                &ids,
            );
        }
    };
    if let Action::Runner {
        command: RunnerCommand::Service(ref service),
        ..
    } = parsed.action
    {
        let mut stderr = io::stderr();
        return prose_runner_core::service::execute(
            service,
            &parsed.globals,
            &system,
            cancellation,
            human_stdout,
            &mut stderr,
        );
    }
    if let Action::Runner { ref command, json } = parsed.action {
        if prose_runner_core::service_account::is_service_command(command) {
            let mode = if json {
                OutputMode::Json
            } else {
                parsed.globals.output.unwrap_or_default()
            };
            return prose_runner_core::service_account::execute_user_command(
                command,
                &system,
                mode,
                cancellation,
            )
            .unwrap_or_else(|error| {
                prose_runner_core::service::argv_error_outcome(args, &error, mode, Some(&system))
                    .unwrap_or_else(|| error_outcome(error, mode, &clock, &ids))
            });
        }
    }
    let config = match resolve_config(&parsed.globals, &system) {
        Ok(config) => config,
        Err(error) => {
            let mode = match parsed.action {
                Action::Runner { json: true, .. } => OutputMode::Json,
                _ => parsed.globals.output.unwrap_or_default(),
            };
            return error_outcome(error, mode, &clock, &ids);
        }
    };
    // The default hosted harness runs no language command, so a language
    // command word that also names a service command is that rejection.
    if let Action::Forward {
        hosted_rejection: Some(ref service),
        ..
    } = parsed.action
    {
        if config.harness.value == "openprose" && !parsed.globals.dry_run {
            let mut stderr = io::stderr();
            return prose_runner_core::service::execute(
                service,
                &parsed.globals,
                &system,
                cancellation,
                human_stdout,
                &mut stderr,
            );
        }
    }
    let mode = prose_runner_core::runner::action_output_mode(&parsed, &config);
    let published_startup = prose_runner_core::kernel_startup::PUBLISHED_KERNEL_STARTUP
        && matches!(parsed.action, Action::Forward { .. })
        && prose_runner_core::installed_adapters::for_harness(&config.harness.value).is_some();
    let selected_image = if published_startup {
        prose_runner_core::kernel_startup::published_kernel(cancellation)
    } else {
        embedded_runtime_image()
    };
    let image = match selected_image {
        Ok(image) => image,
        Err(error) => return error_outcome(error, mode, &clock, &ids),
    };
    execute_with_cancellation_and_human_stream(
        &parsed,
        &config,
        &image,
        &clock,
        &ids,
        cancellation,
        human_stdout,
    )
}

/// Every runner value option (see `invocation::is_value_option`), so a
/// malformed later option still honors an earlier `--output`.
const VALUE_OPTIONS: [&str; 16] = [
    "--harness",
    "--transport",
    "--cwd",
    "--model",
    "--auth-profile",
    "--native-max-turns",
    "--native-timeout",
    "--native-tool-timeout",
    "--native-output-bytes",
    "--native-profile",
    "--native-add-dir",
    "--native-allow-tool",
    "--native-log",
    "--output-contract",
    "--permission-mode",
    "--timeout",
];

fn output_hint(args: &[String]) -> OutputMode {
    let mut mode = OutputMode::Human;
    let mut index = 0;
    while let Some(token) = args.get(index) {
        if token == "--" {
            break;
        }
        if token == "--output" {
            if let Some(value) = args.get(index + 1) {
                if let Ok(recognized) = OutputMode::parse(value) {
                    mode = recognized;
                }
            }
            index += 2;
            continue;
        }
        if let Some(value) = token.strip_prefix("--output=") {
            if let Ok(recognized) = OutputMode::parse(value) {
                mode = recognized;
            }
            index += 1;
            continue;
        }
        if matches!(token.as_str(), "--dry-run" | "--no-color" | "--verbose") {
            index += 1;
            continue;
        }
        if VALUE_OPTIONS.contains(&token.as_str()) {
            index += 2;
            continue;
        }
        if token
            .split_once('=')
            .is_some_and(|(name, _)| VALUE_OPTIONS.contains(&name))
        {
            index += 1;
            continue;
        }
        if token == "cli" && args.last().is_some_and(|last| last == "--json") {
            return OutputMode::Json;
        }
        break;
    }
    mode
}

#[cfg(test)]
mod image_bundle_tests {
    use super::*;

    #[test]
    fn parser_rejects_a_manifest_and_record_path_outside_the_ascii_allowlist() {
        let parsed = parse_embedded_bundle(IMAGE_BUNDLE).unwrap();
        let original = parsed.files.first().unwrap().0.as_bytes().to_vec();
        let mut replacement = original.clone();
        replacement[b"payload/".len()] = b' ';
        let mut tampered = IMAGE_BUNDLE.to_vec();
        let mut replacements = 0;
        for offset in 0..=tampered.len() - original.len() {
            if tampered[offset..offset + original.len()] == original[..] {
                tampered[offset..offset + replacement.len()].copy_from_slice(&replacement);
                replacements += 1;
            }
        }
        assert!(
            replacements >= 2,
            "manifest and record must both be changed"
        );
        let error = parse_embedded_bundle(&tampered).unwrap_err();
        assert!(format!("{error:?}").contains("unsafe path"));
    }
}
