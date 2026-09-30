//! Audited protocol and platform-neutral construction for the Windows process host.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fmt;
use std::io::{self, BufRead};
use std::path::Path;
use std::sync::mpsc::{SyncSender, TrySendError};

pub const COMPONENT_NAME: &str = "openprose-windows-process-host";
pub const COMPONENT_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const REQUEST_SCHEMA: &str = "openprose.windows-process-host.request/1";
pub const CONTROL_SCHEMA: &str = "openprose.windows-process-host.control/1";
pub const EVENT_SCHEMA: &str = "openprose.windows-process-host.event/1";
pub const ERROR_SCHEMA: &str = "openprose.windows-process-host.error/1";
pub const IDENTITY_SCHEMA: &str = "openprose.windows-process-host.identity/1";
pub const MAX_REQUEST_BYTES: usize = 64 * 1_048_576;
pub const MAX_CONTROL_BYTES: usize = 4096;
// One byte below 16 MiB makes the decoded cap divisible by three, so JSON
// Schema can express the exact corresponding padded-base64 maximum length.
pub const MAX_CHILD_STDIN_BYTES: usize = 16 * 1_048_576 - 1;
pub const MAX_STDIN_BASE64_CHARS: usize = 22_369_620;
pub const MAX_ARGV_ITEMS: usize = 64;
pub const MAX_ARGUMENT_CHARS: usize = 32_766;
pub const MAX_COMMAND_LINE_UTF16_UNITS: usize = 32_766;
pub const MAX_ENVIRONMENT_ITEMS: usize = 64;
pub const MAX_ENVIRONMENT_NAME_CHARS: usize = 128;
pub const MAX_ENVIRONMENT_VALUE_CHARS: usize = 8192;
pub const MAX_PATH_CHARS: usize = 32_766;
pub const MAX_ENVIRONMENT_BYTES: usize = 4 * 1_048_576;
// JSON permits one Unicode scalar to be written as an escaped surrogate pair
// (`\uXXXX\uXXXX`), so a schema `maxLength` character can occupy 12 wire bytes.
const MAX_JSON_WIRE_BYTES_PER_CHAR: usize = 12;
const MAX_JSON_FIXED_OVERHEAD: usize = 64 * 1024;
pub const MAX_SCHEMA_REQUEST_BYTES: usize = MAX_STDIN_BASE64_CHARS
    + MAX_ARGV_ITEMS * (MAX_ARGUMENT_CHARS * MAX_JSON_WIRE_BYTES_PER_CHAR + 3)
    + MAX_ENVIRONMENT_ITEMS
        * (MAX_ENVIRONMENT_NAME_CHARS
            + MAX_ENVIRONMENT_VALUE_CHARS * MAX_JSON_WIRE_BYTES_PER_CHAR
            + 64)
    + 3 * MAX_PATH_CHARS * MAX_JSON_WIRE_BYTES_PER_CHAR
    + 128
    + MAX_JSON_FIXED_OVERHEAD;
pub const RUNNER_METADATA_NAMES: [&str; 3] = [
    "OPENPROSE_INVOCATION_ID",
    "OPENPROSE_RECURSION_TOKEN",
    "OPENPROSE_RUN_NONCE",
];

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct HostRequest {
    pub schema: String,
    pub request_id: String,
    pub executable: String,
    pub wrapper_executable: String,
    pub argv: Vec<String>,
    pub cwd: String,
    pub environment: Vec<EnvironmentEntry>,
    pub stdin_base64: String,
    pub limits: HostLimits,
    pub cancellation: CancellationPolicy,
    pub run_timeout_ms: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct EnvironmentEntry {
    pub name: String,
    pub value: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct HostLimits {
    pub max_stdout_bytes: usize,
    pub max_stderr_bytes: usize,
    pub max_queued_chunks: usize,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct CancellationPolicy {
    pub graceful: GracefulControl,
    pub grace_ms: u64,
    pub hard_kill_after_ms: u64,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum GracefulControl {
    CtrlBreak,
    None,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields, tag = "type", rename_all = "kebab-case")]
pub enum HostControl {
    Cancel { schema: String, reason: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolLine {
    Complete(Vec<u8>),
    EndOfStream,
    Unterminated,
    LimitExceeded,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlAction {
    Cancel(String),
    ParentDisconnected,
    ProtocolFailure,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventQueueError {
    Backpressure,
    Disconnected,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EventPhase {
    AwaitingStart,
    Streaming,
    Exited,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventSequencer {
    request_id: String,
    next_sequence: u64,
    phase: EventPhase,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedRequest {
    pub request: HostRequest,
    pub child_stdin: Vec<u8>,
    pub command_line: Vec<u16>,
    pub environment_block: Vec<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationError {
    pub code: &'static str,
    pub field: &'static str,
    pub message: &'static str,
}

impl ValidationError {
    const fn new(code: &'static str, field: &'static str, message: &'static str) -> Self {
        Self {
            code,
            field,
            message,
        }
    }
}

impl fmt::Display for ValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} at {}: {}",
            self.code, self.field, self.message
        )
    }
}

impl std::error::Error for ValidationError {}

impl EventSequencer {
    /// Creates the one-request event state machine.
    ///
    /// # Errors
    ///
    /// Returns a stable protocol error when the request identity is invalid.
    pub fn new(request_id: &str) -> Result<Self, ValidationError> {
        validate_identifier(request_id, "requestId")?;
        Ok(Self {
            request_id: request_id.to_owned(),
            next_sequence: 0,
            phase: EventPhase::AwaitingStart,
        })
    }

    /// Produces the next event while enforcing start/stream/terminal ordering.
    ///
    /// # Errors
    ///
    /// Returns a stable error for a stream before start, duplicate start or
    /// terminal, an event after terminal, or an unknown event kind.
    pub fn next(&mut self, kind: &str, payload: &Value) -> Result<Value, ValidationError> {
        let next_phase = match (self.phase, kind) {
            (EventPhase::AwaitingStart, "host.started")
            | (EventPhase::Streaming, "child.stdout" | "child.stderr") => EventPhase::Streaming,
            (EventPhase::Streaming, "host.exited") => EventPhase::Exited,
            _ => {
                return Err(ValidationError::new(
                    "PROTOCOL_SEQUENCE_INVALID",
                    "event",
                    "event kind is unknown or outside the closed lifecycle order",
                ));
            }
        };
        let value = json!({
            "schema": EVENT_SCHEMA,
            "requestId": self.request_id,
            "sequence": self.next_sequence,
            "type": kind,
            "payload": payload
        });
        self.next_sequence = self.next_sequence.checked_add(1).ok_or_else(|| {
            ValidationError::new(
                "PROTOCOL_SEQUENCE_INVALID",
                "event.sequence",
                "event sequence overflowed",
            )
        })?;
        self.phase = next_phase;
        Ok(value)
    }
}

/// Attempts to enqueue an event without ever blocking the supervision loop.
///
/// # Errors
///
/// Distinguishes a full bounded queue from a disconnected event consumer.
pub fn enqueue_event(sender: &SyncSender<Value>, event: Value) -> Result<(), EventQueueError> {
    match sender.try_send(event) {
        Ok(()) => Ok(()),
        Err(TrySendError::Full(_)) => Err(EventQueueError::Backpressure),
        Err(TrySendError::Disconnected(_)) => Err(EventQueueError::Disconnected),
    }
}

/// Reads one newline-terminated protocol record without allocating beyond its
/// closed content limit.
///
/// # Errors
///
/// Returns the underlying reader error. EOF, partial EOF, and a limit breach
/// remain distinct protocol states.
pub fn read_protocol_line<R: BufRead>(reader: &mut R, maximum: usize) -> io::Result<ProtocolLine> {
    let mut output = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(if output.is_empty() {
                ProtocolLine::EndOfStream
            } else {
                ProtocolLine::Unterminated
            });
        }
        if let Some(position) = available.iter().position(|byte| *byte == b'\n') {
            if position == 0 && output.last() == Some(&b'\r') {
                output.pop();
            }
            let content_end = if position > 0 && available[position - 1] == b'\r' {
                position - 1
            } else {
                position
            };
            let content = &available[..content_end];
            if output.len().saturating_add(content.len()) > maximum {
                reader.consume(position + 1);
                return Ok(ProtocolLine::LimitExceeded);
            }
            output.extend_from_slice(content);
            reader.consume(position + 1);
            return Ok(ProtocolLine::Complete(output));
        }

        let available_len = available.len();
        let combined = output.len().saturating_add(available_len);
        let pending_crlf =
            combined == maximum.saturating_add(1) && available.last() == Some(&b'\r');
        if combined > maximum && !pending_crlf {
            reader.consume(available_len);
            return Ok(ProtocolLine::LimitExceeded);
        }
        output.extend_from_slice(available);
        reader.consume(available_len);
    }
}

#[must_use]
pub fn classify_control_line(line: ProtocolLine) -> ControlAction {
    match line {
        ProtocolLine::Complete(encoded) => match parse_control_line(&encoded) {
            Ok(HostControl::Cancel { reason, .. }) => ControlAction::Cancel(reason),
            Err(_) => ControlAction::ProtocolFailure,
        },
        ProtocolLine::EndOfStream => ControlAction::ParentDisconnected,
        ProtocolLine::Unterminated | ProtocolLine::LimitExceeded => ControlAction::ProtocolFailure,
    }
}

#[must_use]
pub fn host_identity() -> Value {
    json!({
        "schema": IDENTITY_SCHEMA,
        "component": COMPONENT_NAME,
        "componentVersion": COMPONENT_VERSION,
        "target": {
            "os": std::env::consts::OS,
            "architecture": std::env::consts::ARCH,
            "nativeWindowsImplementation": cfg!(windows)
        },
        "protocol": {
            "request": REQUEST_SCHEMA,
            "control": CONTROL_SCHEMA,
            "event": EVENT_SCHEMA,
            "error": ERROR_SCHEMA,
            "maxRequestBytes": MAX_REQUEST_BYTES,
            "maxControlBytes": MAX_CONTROL_BYTES,
            "limits": {
                "maxDecodedStdinBytes": MAX_CHILD_STDIN_BYTES,
                "maxStdinBase64Characters": MAX_STDIN_BASE64_CHARS,
                "maxArgvItems": MAX_ARGV_ITEMS,
                "maxArgumentCharacters": MAX_ARGUMENT_CHARS,
                "maxEnvironmentItems": MAX_ENVIRONMENT_ITEMS,
                "maxEnvironmentNameCharacters": MAX_ENVIRONMENT_NAME_CHARS,
                "maxEnvironmentValueCharacters": MAX_ENVIRONMENT_VALUE_CHARS,
                "maxPathCharacters": MAX_PATH_CHARS,
                "maxEnvironmentBlockBytes": MAX_ENVIRONMENT_BYTES,
                "maxRenderedCommandLineUtf16Units": MAX_COMMAND_LINE_UTF16_UNITS
            }
        },
        "claims": {
            "providerCallsMade": false,
            "nativeWindowsRuntimeEvidence": false,
            "strictWindowsContainmentReady": false
        }
    })
}

impl HostRequest {
    /// Validates the closed host request without consulting ambient process state.
    ///
    /// # Errors
    ///
    /// Returns a stable error when a field violates the closed protocol,
    /// recursion, environment, path, or resource-limit policy.
    pub fn validate(self) -> Result<ValidatedRequest, ValidationError> {
        if self.schema != REQUEST_SCHEMA {
            return Err(ValidationError::new(
                "PROTOCOL_VERSION_UNSUPPORTED",
                "schema",
                "request schema is unsupported",
            ));
        }
        validate_identifier(&self.request_id, "requestId")?;
        validate_executable_path(&self.executable, "executable")?;
        validate_executable_path(&self.wrapper_executable, "wrapperExecutable")?;
        if self
            .executable
            .eq_ignore_ascii_case(&self.wrapper_executable)
        {
            return Err(ValidationError::new(
                "RECURSIVE_INVOCATION",
                "executable",
                "child executable equals the prose wrapper",
            ));
        }
        validate_windows_absolute_path(&self.cwd, "cwd")?;
        if self.argv.len() > MAX_ARGV_ITEMS {
            return Err(ValidationError::new(
                "REQUEST_LIMIT_EXCEEDED",
                "argv",
                "too many child arguments",
            ));
        }
        for argument in &self.argv {
            validate_utf16_input(argument, "argv")?;
            if argument.chars().count() > MAX_ARGUMENT_CHARS {
                return Err(ValidationError::new(
                    "REQUEST_LIMIT_EXCEEDED",
                    "argv",
                    "one child argument exceeds the closed character limit",
                ));
            }
        }
        validate_limits(&self.limits, &self.cancellation, self.run_timeout_ms)?;
        if self.stdin_base64.len() > MAX_STDIN_BASE64_CHARS {
            return Err(ValidationError::new(
                "REQUEST_LIMIT_EXCEEDED",
                "stdinBase64",
                "encoded child stdin exceeds the closed request limit",
            ));
        }
        let child_stdin = decode_base64(&self.stdin_base64)?;
        if child_stdin.len() > MAX_CHILD_STDIN_BYTES {
            return Err(ValidationError::new(
                "REQUEST_LIMIT_EXCEEDED",
                "stdinBase64",
                "decoded child stdin exceeds the host limit",
            ));
        }
        let environment_block = build_environment_block(&self.environment)?;
        let command_line = build_command_line(&self.executable, &self.argv)?;
        Ok(ValidatedRequest {
            request: self,
            child_stdin,
            command_line,
            environment_block,
        })
    }
}

/// Parses and validates one bounded request JSON line.
///
/// # Errors
///
/// Returns a stable error for malformed, oversized, unsupported, recursive,
/// or otherwise inadmissible requests.
pub fn parse_request_line(line: &[u8]) -> Result<ValidatedRequest, ValidationError> {
    if line.len() > MAX_REQUEST_BYTES {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "request",
            "request line exceeds the host limit",
        ));
    }
    let request: HostRequest = serde_json::from_slice(line).map_err(|_| {
        ValidationError::new(
            "PROTOCOL_MALFORMED",
            "request",
            "request is not a closed valid JSON object",
        )
    })?;
    request.validate()
}

/// Parses one closed, versioned control JSON line.
///
/// # Errors
///
/// Returns a stable error for malformed or unsupported controls.
pub fn parse_control_line(line: &[u8]) -> Result<HostControl, ValidationError> {
    if line.len() > MAX_CONTROL_BYTES {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "control",
            "control line exceeds the host limit",
        ));
    }
    let control: HostControl = serde_json::from_slice(line).map_err(|_| {
        ValidationError::new(
            "PROTOCOL_MALFORMED",
            "control",
            "control is not a closed valid JSON object",
        )
    })?;
    match &control {
        HostControl::Cancel { schema, reason } => {
            if schema != CONTROL_SCHEMA {
                return Err(ValidationError::new(
                    "PROTOCOL_VERSION_UNSUPPORTED",
                    "control.schema",
                    "control schema is unsupported",
                ));
            }
            validate_identifier(reason, "control.reason")?;
        }
    }
    Ok(control)
}

fn validate_identifier(value: &str, field: &'static str) -> Result<(), ValidationError> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(ValidationError::new(
            "PROTOCOL_MALFORMED",
            field,
            "identifier is empty, too long, or contains a forbidden byte",
        ));
    }
    Ok(())
}

fn validate_executable_path(value: &str, field: &'static str) -> Result<(), ValidationError> {
    validate_windows_absolute_path(value, field)?;
    if !Path::new(value)
        .extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("exe"))
    {
        return Err(ValidationError::new(
            "SHELL_TARGET_REJECTED",
            field,
            "only an explicit .exe application is accepted",
        ));
    }
    Ok(())
}

fn validate_windows_absolute_path(value: &str, field: &'static str) -> Result<(), ValidationError> {
    validate_utf16_input(value, field)?;
    if value.chars().count() > MAX_PATH_CHARS {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            field,
            "path exceeds the closed character limit",
        ));
    }
    let bytes = value.as_bytes();
    let drive_absolute = bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/');
    let unc_absolute = value.starts_with("\\\\")
        && value[2..]
            .split(['\\', '/'])
            .filter(|part| !part.is_empty())
            .count()
            >= 2;
    if !drive_absolute && !unc_absolute {
        return Err(ValidationError::new(
            "PATH_INVALID",
            field,
            "a Windows drive-absolute or UNC path is required",
        ));
    }
    Ok(())
}

fn validate_utf16_input(value: &str, field: &'static str) -> Result<(), ValidationError> {
    if value.contains('\0') {
        return Err(ValidationError::new(
            "PROTOCOL_MALFORMED",
            field,
            "NUL is forbidden",
        ));
    }
    Ok(())
}

fn validate_limits(
    limits: &HostLimits,
    cancellation: &CancellationPolicy,
    run_timeout_ms: u64,
) -> Result<(), ValidationError> {
    if !(1..=256 * 1_048_576).contains(&limits.max_stdout_bytes)
        || !(1..=64 * 1_048_576).contains(&limits.max_stderr_bytes)
        || !(1..=1024).contains(&limits.max_queued_chunks)
    {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "limits",
            "stream or queue limit is outside the closed host range",
        ));
    }
    if run_timeout_ms == 0
        || run_timeout_ms > 86_400_000
        || cancellation.grace_ms > 60_000
        || cancellation.hard_kill_after_ms == 0
        || cancellation.hard_kill_after_ms > 60_000
    {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "timeouts",
            "timeout is outside the closed host range",
        ));
    }
    Ok(())
}

/// Produces an explicit, sorted, double-NUL-terminated Unicode environment block.
///
/// # Errors
///
/// Returns a stable error for malformed or duplicate entries, missing runner
/// metadata, or an environment beyond the closed size limit.
pub fn build_environment_block(entries: &[EnvironmentEntry]) -> Result<Vec<u16>, ValidationError> {
    if entries.len() > MAX_ENVIRONMENT_ITEMS {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "environment",
            "too many environment entries",
        ));
    }
    let mut sorted = BTreeMap::new();
    for entry in entries {
        if entry.name.is_empty()
            || entry.name.len() > MAX_ENVIRONMENT_NAME_CHARS
            || !entry.name.is_ascii()
            || !entry
                .name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
            || entry.name.as_bytes()[0].is_ascii_digit()
        {
            return Err(ValidationError::new(
                "ENVIRONMENT_INVALID",
                "environment.name",
                "environment name is outside the closed ASCII form",
            ));
        }
        validate_utf16_input(&entry.value, "environment.value")?;
        if entry.value.chars().count() > MAX_ENVIRONMENT_VALUE_CHARS {
            return Err(ValidationError::new(
                "REQUEST_LIMIT_EXCEEDED",
                "environment.value",
                "environment value exceeds the closed character limit",
            ));
        }
        let key = entry.name.to_ascii_uppercase();
        if sorted.insert(key, entry).is_some() {
            return Err(ValidationError::new(
                "ENVIRONMENT_INVALID",
                "environment.name",
                "environment contains a duplicate case-insensitive name",
            ));
        }
    }
    for required in RUNNER_METADATA_NAMES {
        let Some(entry) = sorted.get(required) else {
            return Err(ValidationError::new(
                "RECURSION_METADATA_MISSING",
                "environment",
                "required runner identity metadata is absent",
            ));
        };
        if entry.value.is_empty() {
            return Err(ValidationError::new(
                "RECURSION_METADATA_MISSING",
                "environment",
                "required runner identity metadata is empty",
            ));
        }
    }
    let mut block = Vec::new();
    for entry in sorted.values() {
        block.extend(entry.name.encode_utf16());
        block.push(u16::from(b'='));
        block.extend(entry.value.encode_utf16());
        block.push(0);
    }
    block.push(0);
    if block.len().saturating_mul(2) > MAX_ENVIRONMENT_BYTES {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "environment",
            "Unicode environment block exceeds the host limit",
        ));
    }
    Ok(block)
}

/// Quotes one argument using the Windows C-runtime parsing convention.
///
/// # Errors
///
/// Returns a stable error when the argument contains a NUL.
pub fn quote_windows_argument(argument: &str) -> Result<String, ValidationError> {
    validate_utf16_input(argument, "argv")?;
    if !argument.is_empty()
        && !argument
            .chars()
            .any(|character| character.is_whitespace() || character == '"')
    {
        return Ok(argument.to_owned());
    }
    let mut quoted = String::from("\"");
    let mut backslashes = 0usize;
    for character in argument.chars() {
        if character == '\\' {
            backslashes += 1;
            continue;
        }
        if character == '"' {
            quoted.extend(std::iter::repeat_n('\\', backslashes * 2 + 1));
            quoted.push('"');
        } else {
            quoted.extend(std::iter::repeat_n('\\', backslashes));
            quoted.push(character);
        }
        backslashes = 0;
    }
    quoted.extend(std::iter::repeat_n('\\', backslashes * 2));
    quoted.push('"');
    Ok(quoted)
}

/// Renders an application and argument vector as one bounded, NUL-terminated
/// Windows command-line buffer.
///
/// # Errors
///
/// Returns a stable error for invalid input or an oversized result.
pub fn build_command_line(executable: &str, argv: &[String]) -> Result<Vec<u16>, ValidationError> {
    let mut rendered = quote_windows_argument(executable)?;
    for argument in argv {
        rendered.push(' ');
        rendered.push_str(&quote_windows_argument(argument)?);
    }
    let mut wide: Vec<u16> = rendered.encode_utf16().collect();
    if wide.len() > MAX_COMMAND_LINE_UTF16_UNITS {
        return Err(ValidationError::new(
            "REQUEST_LIMIT_EXCEEDED",
            "argv",
            "rendered Windows command line exceeds 32766 UTF-16 code units",
        ));
    }
    wide.push(0);
    Ok(wide)
}

/// Encodes bytes with canonical padded RFC 4648 base64.
#[must_use]
pub fn encode_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let first = chunk[0];
        let second = *chunk.get(1).unwrap_or(&0);
        let third = *chunk.get(2).unwrap_or(&0);
        output.push(char::from(TABLE[usize::from(first >> 2)]));
        output.push(char::from(
            TABLE[usize::from((first & 0x03) << 4 | second >> 4)],
        ));
        if chunk.len() > 1 {
            output.push(char::from(
                TABLE[usize::from((second & 0x0f) << 2 | third >> 6)],
            ));
        } else {
            output.push('=');
        }
        if chunk.len() > 2 {
            output.push(char::from(TABLE[usize::from(third & 0x3f)]));
        } else {
            output.push('=');
        }
    }
    output
}

/// Decodes canonical padded RFC 4648 base64.
///
/// # Errors
///
/// Returns a stable error for noncanonical or malformed input.
pub fn decode_base64(text: &str) -> Result<Vec<u8>, ValidationError> {
    if !text.len().is_multiple_of(4) || !text.is_ascii() {
        return Err(base64_error());
    }
    let mut output = Vec::with_capacity(text.len() / 4 * 3);
    for (chunk_index, chunk) in text.as_bytes().chunks(4).enumerate() {
        let last = chunk_index + 1 == text.len() / 4;
        let a = base64_value(chunk[0])?;
        let b = base64_value(chunk[1])?;
        let c_pad = chunk[2] == b'=';
        let d_pad = chunk[3] == b'=';
        if c_pad && !d_pad || d_pad && !last {
            return Err(base64_error());
        }
        let c = if c_pad { 0 } else { base64_value(chunk[2])? };
        let d = if d_pad { 0 } else { base64_value(chunk[3])? };
        if c_pad && b & 0x0f != 0 || d_pad && !c_pad && c & 0x03 != 0 {
            return Err(base64_error());
        }
        output.push(a << 2 | b >> 4);
        if !c_pad {
            output.push(b << 4 | c >> 2);
        }
        if !d_pad {
            output.push(c << 6 | d);
        }
    }
    Ok(output)
}

fn base64_value(byte: u8) -> Result<u8, ValidationError> {
    match byte {
        b'A'..=b'Z' => Ok(byte - b'A'),
        b'a'..=b'z' => Ok(byte - b'a' + 26),
        b'0'..=b'9' => Ok(byte - b'0' + 52),
        b'+' => Ok(62),
        b'/' => Ok(63),
        _ => Err(base64_error()),
    }
}

const fn base64_error() -> ValidationError {
    ValidationError::new(
        "PROTOCOL_MALFORMED",
        "stdinBase64",
        "base64 is not canonical RFC 4648 encoding",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn environment() -> Vec<EnvironmentEntry> {
        vec![
            EnvironmentEntry {
                name: "OPENPROSE_RUN_NONCE".into(),
                value: "nonce".into(),
            },
            EnvironmentEntry {
                name: "Path".into(),
                value: "C:\\bin".into(),
            },
            EnvironmentEntry {
                name: "OPENPROSE_INVOCATION_ID".into(),
                value: "invocation".into(),
            },
            EnvironmentEntry {
                name: "OPENPROSE_RECURSION_TOKEN".into(),
                value: "recursion".into(),
            },
        ]
    }

    fn request() -> HostRequest {
        HostRequest {
            schema: REQUEST_SCHEMA.into(),
            request_id: "request-1".into(),
            executable: "C:\\tools\\agent.exe".into(),
            wrapper_executable: "C:\\tools\\prose.exe".into(),
            argv: vec!["雪 and spaces".into(), "a\\\"b".into(), String::new()],
            cwd: "C:\\workspace".into(),
            environment: environment(),
            stdin_base64: encode_base64(b"stdin\0bytes"),
            limits: HostLimits {
                max_stdout_bytes: 1024,
                max_stderr_bytes: 1024,
                max_queued_chunks: 4,
            },
            cancellation: CancellationPolicy {
                graceful: GracefulControl::CtrlBreak,
                grace_ms: 100,
                hard_kill_after_ms: 1000,
            },
            run_timeout_ms: 5000,
        }
    }

    #[test]
    fn windows_argument_quoting_preserves_boundaries() {
        let cases = [
            ("", "\"\""),
            ("plain", "plain"),
            ("two words", "\"two words\""),
            ("a\\b", "a\\b"),
            ("a\"b", "\"a\\\"b\""),
            ("ends slash \\", "\"ends slash \\\\\""),
            ("雪 λ", "\"雪 λ\""),
        ];
        for (input, expected) in cases {
            assert_eq!(
                quote_windows_argument(input).unwrap(),
                expected,
                "{input:?}"
            );
        }
    }

    #[test]
    fn command_line_is_nul_terminated_and_bounded() {
        let built = build_command_line(
            "C:\\Program Files\\agent.exe",
            &["one".into(), "two words".into()],
        )
        .unwrap();
        assert_eq!(built.last(), Some(&0));
        let decoded = String::from_utf16(&built[..built.len() - 1]).unwrap();
        assert_eq!(
            decoded,
            "\"C:\\Program Files\\agent.exe\" one \"two words\""
        );
    }

    #[test]
    fn environment_is_case_insensitively_sorted_and_double_terminated() {
        let block = build_environment_block(&environment()).unwrap();
        assert!(block.ends_with(&[0, 0]));
        let rendered = String::from_utf16(&block[..block.len() - 1]).unwrap();
        assert!(rendered.starts_with("OPENPROSE_INVOCATION_ID=invocation\0"));
        assert!(rendered.ends_with("Path=C:\\bin\0"));
    }

    #[test]
    fn environment_rejects_case_aliases_and_missing_recursion_metadata() {
        let mut duplicate = environment();
        duplicate.push(EnvironmentEntry {
            name: "PATH".into(),
            value: "C:\\other".into(),
        });
        assert_eq!(
            build_environment_block(&duplicate).unwrap_err().code,
            "ENVIRONMENT_INVALID"
        );
        let missing: Vec<_> = environment()
            .into_iter()
            .filter(|entry| entry.name != "OPENPROSE_RUN_NONCE")
            .collect();
        assert_eq!(
            build_environment_block(&missing).unwrap_err().code,
            "RECURSION_METADATA_MISSING"
        );
    }

    #[test]
    fn base64_round_trips_and_rejects_noncanonical_input() {
        for bytes in [b"".as_slice(), b"f", b"fo", b"foo", b"\0\xff\x10"] {
            assert_eq!(decode_base64(&encode_base64(bytes)).unwrap(), bytes);
        }
        for invalid in ["A", "A===", "Zh==", "Zm9="] {
            assert!(decode_base64(invalid).is_err(), "{invalid}");
        }
    }

    #[test]
    fn request_is_closed_and_rejects_wrapper_recursion() {
        let validated = request().validate().unwrap();
        assert_eq!(validated.child_stdin, b"stdin\0bytes");
        let mut recursive = request();
        recursive.executable = recursive.wrapper_executable.to_ascii_uppercase();
        assert_eq!(
            recursive.validate().unwrap_err().code,
            "RECURSIVE_INVOCATION"
        );

        let mut encoded = serde_json::to_value(request()).unwrap();
        encoded["invented"] = serde_json::json!(true);
        assert_eq!(
            parse_request_line(&serde_json::to_vec(&encoded).unwrap())
                .unwrap_err()
                .code,
            "PROTOCOL_MALFORMED"
        );
    }

    #[test]
    fn controls_are_closed_and_versioned() {
        let valid = serde_json::json!({
            "type": "cancel",
            "schema": CONTROL_SCHEMA,
            "reason": "caller"
        });
        assert!(parse_control_line(&serde_json::to_vec(&valid).unwrap()).is_ok());
        let extra = serde_json::json!({
            "type": "cancel",
            "schema": CONTROL_SCHEMA,
            "reason": "caller",
            "killPid": 42
        });
        assert_eq!(
            parse_control_line(&serde_json::to_vec(&extra).unwrap())
                .unwrap_err()
                .code,
            "PROTOCOL_MALFORMED"
        );
    }

    #[test]
    fn schema_maximum_wire_representation_fits_the_bounded_request_line() {
        assert_eq!(MAX_SCHEMA_REQUEST_BYTES, 55_083_084);
        assert_eq!(MAX_REQUEST_BYTES, 64 * 1_048_576);
        assert_eq!(MAX_CHILD_STDIN_BYTES, 16 * 1_048_576 - 1);
        assert_eq!(MAX_STDIN_BASE64_CHARS, 22_369_620);

        let schema: Value =
            serde_json::from_str(include_str!("../protocol/request.schema.json")).unwrap();
        assert_eq!(schema["properties"]["argv"]["maxItems"], MAX_ARGV_ITEMS);
        assert_eq!(
            schema["properties"]["argv"]["items"]["maxLength"],
            MAX_ARGUMENT_CHARS
        );
        assert_eq!(
            schema["properties"]["environment"]["maxItems"],
            MAX_ENVIRONMENT_ITEMS
        );
        assert_eq!(
            schema["properties"]["environment"]["items"]["properties"]["value"]["maxLength"],
            MAX_ENVIRONMENT_VALUE_CHARS
        );
        assert_eq!(
            schema["properties"]["stdinBase64"]["maxLength"],
            MAX_STDIN_BASE64_CHARS
        );
    }

    #[test]
    fn every_in_memory_request_field_obeys_the_closed_wire_budget() {
        let mut too_many_arguments = request();
        too_many_arguments.argv = vec![String::new(); MAX_ARGV_ITEMS + 1];
        assert_eq!(too_many_arguments.validate().unwrap_err().field, "argv");

        let mut long_argument = request();
        long_argument.argv = vec!["x".repeat(MAX_ARGUMENT_CHARS + 1)];
        assert_eq!(long_argument.validate().unwrap_err().field, "argv");

        let mut long_environment = request();
        long_environment.environment[0].value = "x".repeat(MAX_ENVIRONMENT_VALUE_CHARS + 1);
        assert_eq!(
            long_environment.validate().unwrap_err().field,
            "environment.value"
        );

        let mut oversized_base64 = request();
        oversized_base64.stdin_base64 = "A".repeat(MAX_STDIN_BASE64_CHARS + 4);
        assert_eq!(
            oversized_base64.validate().unwrap_err().field,
            "stdinBase64"
        );
    }

    #[test]
    fn bounded_lines_distinguish_complete_disconnect_partial_and_limit() {
        use std::io::{BufReader, Cursor};

        let mut input = Cursor::new(b"one\r\ntwo\n".to_vec());
        assert_eq!(
            read_protocol_line(&mut input, 3).unwrap(),
            ProtocolLine::Complete(b"one".to_vec())
        );
        assert_eq!(
            read_protocol_line(&mut input, 3).unwrap(),
            ProtocolLine::Complete(b"two".to_vec())
        );
        assert_eq!(
            read_protocol_line(&mut input, 3).unwrap(),
            ProtocolLine::EndOfStream
        );

        assert_eq!(
            read_protocol_line(&mut Cursor::new(b"partial".to_vec()), 16).unwrap(),
            ProtocolLine::Unterminated
        );
        assert_eq!(
            read_protocol_line(&mut Cursor::new(b"1234\n".to_vec()), 3).unwrap(),
            ProtocolLine::LimitExceeded
        );

        let mut split_crlf = BufReader::with_capacity(2, Cursor::new(b"one\r\n".to_vec()));
        assert_eq!(
            read_protocol_line(&mut split_crlf, 3).unwrap(),
            ProtocolLine::Complete(b"one".to_vec())
        );
        let mut false_crlf = BufReader::with_capacity(2, Cursor::new(b"one\rX\n".to_vec()));
        assert_eq!(
            read_protocol_line(&mut false_crlf, 3).unwrap(),
            ProtocolLine::LimitExceeded
        );
    }

    #[test]
    fn control_ingress_fails_partial_or_malformed_lines_closed() {
        assert_eq!(
            classify_control_line(ProtocolLine::EndOfStream),
            ControlAction::ParentDisconnected
        );
        assert_eq!(
            classify_control_line(ProtocolLine::Unterminated),
            ControlAction::ProtocolFailure
        );
        assert_eq!(
            classify_control_line(ProtocolLine::Complete(b"{}".to_vec())),
            ControlAction::ProtocolFailure
        );
        let cancel = serde_json::to_vec(&serde_json::json!({
            "type":"cancel",
            "schema":CONTROL_SCHEMA,
            "reason":"caller"
        }))
        .unwrap();
        assert_eq!(
            classify_control_line(ProtocolLine::Complete(cancel)),
            ControlAction::Cancel("caller".into())
        );
    }

    #[test]
    fn event_sequence_is_monotonic_closed_and_terminal() {
        let mut sequence = EventSequencer::new("request-1").unwrap();
        assert_eq!(
            sequence
                .next("child.stdout", &serde_json::json!({}))
                .unwrap_err()
                .code,
            "PROTOCOL_SEQUENCE_INVALID"
        );
        let started = sequence
            .next("host.started", &serde_json::json!({"pid":42}))
            .unwrap();
        let stream = sequence
            .next("child.stdout", &serde_json::json!({"data":""}))
            .unwrap();
        let exited = sequence
            .next("host.exited", &serde_json::json!({"cleanupVerified":true}))
            .unwrap();
        assert_eq!(started["sequence"], 0);
        assert_eq!(stream["sequence"], 1);
        assert_eq!(exited["sequence"], 2);
        assert_eq!(
            sequence
                .next("host.exited", &serde_json::json!({}))
                .unwrap_err()
                .code,
            "PROTOCOL_SEQUENCE_INVALID"
        );
    }

    #[test]
    fn bounded_event_queue_distinguishes_pressure_from_disconnect() {
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        enqueue_event(&sender, serde_json::json!({"sequence":0})).unwrap();
        assert_eq!(
            enqueue_event(&sender, serde_json::json!({"sequence":1})),
            Err(EventQueueError::Backpressure)
        );
        drop(receiver);
        assert_eq!(
            enqueue_event(&sender, serde_json::json!({"sequence":2})),
            Err(EventQueueError::Disconnected)
        );
    }
}

#[test]
fn output_budget_keeps_host_stderr_boundary() {
    let c = CancellationPolicy {
        graceful: GracefulControl::None,
        grace_ms: 0,
        hard_kill_after_ms: 1,
    };
    let mut l = HostLimits {
        max_stdout_bytes: 268_435_456,
        max_stderr_bytes: 67_108_864,
        max_queued_chunks: 1,
    };
    assert!(validate_limits(&l, &c, 1000).is_ok());
    l.max_stdout_bytes += 1;
    assert!(validate_limits(&l, &c, 1000).is_err());
    l.max_stdout_bytes = 268_435_456;
    l.max_stderr_bytes += 1;
    assert!(validate_limits(&l, &c, 1000).is_err());
}
