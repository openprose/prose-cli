use crate::{Clock, IdSource, OutputMode, RunnerError};
use serde::Serialize;
use serde_json::{Value, json};
use std::io::{self, Write};

#[derive(Debug, Clone)]
pub enum Payload {
    Human { stdout: String, stderr: String },
    Json(Value),
    JsonWithDiagnostic { value: Value, stderr: String },
    Jsonl(Vec<Value>),
    JsonlWithDiagnostic { values: Vec<Value>, stderr: String },
}

#[derive(Debug, Clone)]
pub struct CommandOutcome {
    pub exit_code: u8,
    pub payload: Payload,
    /// Stderr text written before anything else (a dev-endpoint build's
    /// custom-endpoint header), so it is always the first line a person sees.
    pub preamble: String,
}

impl CommandOutcome {
    #[must_use]
    pub fn human(stdout: impl Into<String>, stderr: impl Into<String>, exit_code: u8) -> Self {
        Self {
            exit_code,
            preamble: String::new(),
            payload: Payload::Human {
                stdout: stdout.into(),
                stderr: stderr.into(),
            },
        }
    }

    #[must_use]
    /// Serializes an internally constructed stable result.
    ///
    /// # Panics
    ///
    /// Panics only if a runner-owned `Serialize` implementation fails.
    pub fn json(value: impl Serialize, exit_code: u8) -> Self {
        Self {
            exit_code,
            preamble: String::new(),
            payload: Payload::Json(
                serde_json::to_value(value).expect("serializable runner output"),
            ),
        }
    }

    #[must_use]
    /// Serializes a stable JSON result while retaining a separately sanitized
    /// harness diagnostic on stderr.
    ///
    /// # Panics
    ///
    /// Panics only if a runner-owned `Serialize` implementation fails.
    pub fn json_with_diagnostic(
        value: impl Serialize,
        stderr: impl Into<String>,
        exit_code: u8,
    ) -> Self {
        Self {
            exit_code,
            preamble: String::new(),
            payload: Payload::JsonWithDiagnostic {
                value: serde_json::to_value(value).expect("serializable runner output"),
                stderr: stderr.into(),
            },
        }
    }

    #[must_use]
    pub fn jsonl(values: Vec<Value>, exit_code: u8) -> Self {
        Self {
            exit_code,
            preamble: String::new(),
            payload: Payload::Jsonl(values),
        }
    }

    #[must_use]
    pub fn jsonl_with_diagnostic(
        values: Vec<Value>,
        stderr: impl Into<String>,
        exit_code: u8,
    ) -> Self {
        Self {
            exit_code,
            preamble: String::new(),
            payload: Payload::JsonlWithDiagnostic {
                values,
                stderr: stderr.into(),
            },
        }
    }

    /// Writes `text` to stderr before the payload.
    #[must_use]
    pub fn with_preamble(mut self, text: impl Into<String>) -> Self {
        self.preamble = text.into();
        self
    }

    /// Writes the payload while preserving the stdout/stderr contract.
    ///
    /// # Errors
    ///
    /// Returns the first stream write or JSON serialization error.
    pub fn render(&self, stdout: &mut dyn Write, stderr: &mut dyn Write) -> io::Result<()> {
        if !self.preamble.is_empty() {
            stderr.write_all(self.preamble.as_bytes())?;
            stderr.flush()?;
        }
        match &self.payload {
            Payload::Human {
                stdout: out,
                stderr: err,
            } => {
                stdout.write_all(out.as_bytes())?;
                stderr.write_all(err.as_bytes())?;
            }
            Payload::Json(value) => {
                stdout.write_all(canonical_json(value).as_bytes())?;
                stdout.write_all(b"\n")?;
            }
            Payload::JsonWithDiagnostic { value, stderr: err } => {
                stdout.write_all(canonical_json(value).as_bytes())?;
                stdout.write_all(b"\n")?;
                stderr.write_all(err.as_bytes())?;
            }
            Payload::Jsonl(values) => {
                for value in values {
                    stdout.write_all(canonical_json(value).as_bytes())?;
                    stdout.write_all(b"\n")?;
                }
            }
            Payload::JsonlWithDiagnostic {
                values,
                stderr: err,
            } => {
                for value in values {
                    stdout.write_all(canonical_json(value).as_bytes())?;
                    stdout.write_all(b"\n")?;
                }
                stderr.write_all(err.as_bytes())?;
            }
        }
        Ok(())
    }
}

#[must_use]
pub fn error_outcome(
    error: RunnerError,
    mode: OutputMode,
    clock: &dyn Clock,
    ids: &dyn IdSource,
) -> CommandOutcome {
    let exit_code = error.exit_code;
    match mode {
        OutputMode::Human => CommandOutcome::human("", format!("{error}\n"), exit_code),
        OutputMode::Json => CommandOutcome::json(error, exit_code),
        OutputMode::Jsonl => {
            let event = json!({
                "schema": "openprose.normalized-event/1",
                "sequence": 0,
                "timestamp": clock.now_rfc3339(),
                "invocationId": ids.next_invocation_id(),
                "type": "runner.failed",
                "payload": {
                    "kind": "runner.failed",
                    "error": error
                }
            });
            CommandOutcome::jsonl(vec![event], exit_code)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ErrorCode;

    struct FixedClock;
    impl Clock for FixedClock {
        fn now_rfc3339(&self) -> String {
            "2026-01-02T03:04:05.000Z".into()
        }
    }
    struct FixedId;
    impl IdSource for FixedId {
        fn next_invocation_id(&self) -> String {
            "test-invocation".into()
        }
    }

    #[test]
    fn json_is_one_line_on_stdout_and_never_stderr() {
        let outcome = error_outcome(
            RunnerError::new(
                ErrorCode::HostedUnavailable,
                "hosted-service",
                "down",
                "retry",
            ),
            OutputMode::Json,
            &FixedClock,
            &FixedId,
        );
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        outcome.render(&mut stdout, &mut stderr).unwrap();
        assert!(stderr.is_empty());
        assert_eq!(
            String::from_utf8(stdout.clone()).unwrap().lines().count(),
            1
        );
        let parsed: Value = serde_json::from_slice(&stdout).unwrap();
        assert_eq!(parsed["code"], "HOSTED_UNAVAILABLE");
    }

    #[test]
    fn human_error_uses_only_stderr() {
        let outcome = error_outcome(
            RunnerError::config("bad value"),
            OutputMode::Human,
            &FixedClock,
            &FixedId,
        );
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        outcome.render(&mut stdout, &mut stderr).unwrap();
        assert!(stdout.is_empty());
        assert!(
            String::from_utf8(stderr)
                .unwrap()
                .contains("CONFIG_INVALID")
        );
    }
}

/// Renders a shared human layout (for example
/// `shared/fixtures/human/dry-run.v1.txt`): `{name}` placeholders take
/// already terminal-safe values, a line starting with `?` is printed only when
/// every placeholder in it has a value, and a line starting with `*` repeats
/// once per item of its single list placeholder. Both ports implement the
/// same rules (`shared/fixtures/human/dry-run.v1.json`).
#[must_use]
pub fn render_human_template(
    template: &str,
    values: &std::collections::BTreeMap<&str, Option<String>>,
    lists: &std::collections::BTreeMap<&str, Vec<String>>,
) -> String {
    fn placeholders(line: &str) -> Vec<&str> {
        let mut names = Vec::new();
        let mut rest = line;
        while let Some(start) = rest.find('{') {
            let Some(length) = rest[start + 1..].find('}') else {
                break;
            };
            names.push(&rest[start + 1..start + 1 + length]);
            rest = &rest[start + 2 + length..];
        }
        names
    }
    let mut output = String::new();
    for line in template.lines() {
        if let Some(line) = line.strip_prefix('*') {
            let name = placeholders(line).into_iter().next().unwrap_or_default();
            for item in lists.get(name).into_iter().flatten() {
                output.push_str(&line.replace(&format!("{{{name}}}"), item));
                output.push('\n');
            }
            continue;
        }
        let (optional, line) = line
            .strip_prefix('?')
            .map_or((false, line), |rest| (true, rest));
        let mut rendered = line.to_owned();
        let mut complete = true;
        for name in placeholders(line) {
            match values.get(name).and_then(Option::as_deref) {
                Some(value) => rendered = rendered.replace(&format!("{{{name}}}"), value),
                None => complete = false,
            }
        }
        if complete || !optional {
            output.push_str(&rendered);
            output.push('\n');
        }
    }
    output
}

#[cfg(test)]
mod template_tests {
    use super::render_human_template;
    use serde_json::Value;

    #[test]
    fn canonical_json_matches_the_shared_vectors() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/human/json-output.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            let input: Value = serde_json::from_str(case["input"].as_str().unwrap()).unwrap();
            assert_eq!(
                super::canonical_json(&input),
                case["output"].as_str().unwrap(),
                "{}",
                case["id"]
            );
        }
        for (value, text) in [
            (1e-6, "0.000001"),
            (123.456, "123.456"),
            (1e20, "100000000000000000000"),
            (2.5e-7, "2.5e-7"),
        ] {
            assert_eq!(super::ecmascript_number(value), text);
        }
    }

    #[test]
    fn dry_run_template_matches_the_shared_vectors() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/human/dry-run.v1.json"
        ))
        .unwrap();
        let template = include_str!("../../../../shared/fixtures/human/dry-run.v1.txt");
        for case in fixture["cases"].as_array().unwrap() {
            let mut values = std::collections::BTreeMap::new();
            let mut lists = std::collections::BTreeMap::new();
            for (name, value) in case["values"].as_object().unwrap() {
                if let Some(items) = value.as_array() {
                    lists.insert(
                        name.as_str(),
                        items
                            .iter()
                            .map(|item| item.as_str().unwrap().to_owned())
                            .collect(),
                    );
                } else {
                    values.insert(name.as_str(), value.as_str().map(str::to_owned));
                }
            }
            assert_eq!(
                render_human_template(template, &values, &lists),
                case["rendered"].as_str().unwrap(),
                "{}",
                case["id"]
            );
        }
    }
}

/// Compact JSON exactly as the Bun build prints it (`JSON.stringify` of an
/// object whose keys are sorted): object keys in UTF-16 code-unit order and
/// numbers in ECMAScript `Number::toString` form (`100`, not `100.0`;
/// `10000000000000000`, not `1e16`). Both ports check it against
/// `shared/fixtures/human/json-output.json`.
#[must_use]
pub fn canonical_json(value: &Value) -> String {
    let mut out = String::new();
    write_canonical(value, &mut out);
    out
}

fn write_canonical(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                out.push_str(&integer.to_string());
            } else if let Some(integer) = number.as_u64() {
                out.push_str(&integer.to_string());
            } else {
                out.push_str(&ecmascript_number(number.as_f64().unwrap_or(f64::NAN)));
            }
        }
        Value::String(text) => out.push_str(&Value::String(text.clone()).to_string()),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        Value::Object(object) => {
            let mut keys: Vec<&String> = object.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            out.push('{');
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push_str(&Value::String(key.clone()).to_string());
                out.push(':');
                write_canonical(&object[key], out);
            }
            out.push('}');
        }
    }
}

/// ECMAScript `Number::toString(x)` (radix 10) for a finite `x`; a
/// non-finite value is `null`, as `JSON.stringify` writes it.
///
/// # Panics
///
/// Never: Rust's exponential float form always carries an exponent.
#[must_use]
pub fn ecmascript_number(x: f64) -> String {
    if !x.is_finite() {
        return "null".to_owned();
    }
    if x == 0.0 {
        return "0".to_owned();
    }
    if x < 0.0 {
        return format!("-{}", ecmascript_number(-x));
    }
    // Rust's shortest round-trip digits: `d.ddddde±n`.
    let exponential = format!("{x:e}");
    let (mantissa, exponent) = exponential
        .split_once('e')
        .expect("exponential form has an exponent");
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let k = i64::try_from(digits.len()).unwrap_or(i64::MAX);
    let n = exponent.parse::<i64>().unwrap_or(0) + 1;
    let zeros = |count: i64| "0".repeat(usize::try_from(count).unwrap_or(0));
    if k <= n && n <= 21 {
        format!("{digits}{}", zeros(n - k))
    } else if 0 < n && n <= 21 {
        let split = usize::try_from(n).unwrap_or(0);
        format!("{}.{}", &digits[..split], &digits[split..])
    } else if -6 < n && n <= 0 {
        format!("0.{}{digits}", zeros(-n))
    } else {
        let sign = if n - 1 < 0 { '-' } else { '+' };
        let magnitude = (n - 1).abs();
        if k == 1 {
            format!("{digits}e{sign}{magnitude}")
        } else {
            format!("{}.{}e{sign}{magnitude}", &digits[..1], &digits[1..])
        }
    }
}
