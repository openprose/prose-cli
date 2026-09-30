//! Data-only package preparation and verified registry consumption.
use crate::error::ErrorCode;
use crate::output::CommandOutcome;
use crate::service::Environment;
use crate::service_account::Session;
use crate::{CancellationToken, OutputMode, RunnerError};
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

pub(crate) const LIMIT: usize = 2 * 1024 * 1024;
const FILE_LIMIT: usize = 256 * 1024;
const TOTAL_LIMIT: usize = 1024 * 1024;
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PackageCommand {
    Publish {
        source: String,
        organization: String,
        name: String,
        version: String,
        public: bool,
    },
    Fetch {
        reference: String,
        destination: String,
        sha256: Option<String>,
    },
    List {
        organization: String,
        cursor: Option<String>,
    },
    Withdraw {
        reference: String,
    },
    /// A known command whose arguments are invalid: `reason` is reported in
    /// the service-operation envelope before any request.
    Invalid {
        operation: &'static str,
        reason: String,
    },
}
fn invalid() -> RunnerError {
    RunnerError::config("Invalid or unsafe package input.")
}
fn protocol() -> RunnerError {
    RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
}
fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn text(value: &Value) -> Result<&str, RunnerError> {
    value.as_str().ok_or_else(invalid)
}
fn fields(value: &Value, allowed: &[&str], required: &[&str]) -> Result<(), RunnerError> {
    let map = value.as_object().ok_or_else(invalid)?;
    if map.keys().any(|key| !allowed.contains(&key.as_str()))
        || required.iter().any(|key| !map.contains_key(*key))
    {
        return Err(invalid());
    }
    Ok(())
}
fn exact(value: &Value, names: &[&str]) -> Result<(), RunnerError> {
    fields(value, names, names)
}
fn slug(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !value.starts_with('-')
        && !value.ends_with('-')
}
fn digits(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().all(|b| b.is_ascii_digit())
        && (value == "0" || !value.starts_with('0'))
}
fn version(value: &str) -> bool {
    if value.len() > 128 {
        return false;
    }
    let (base, build) = value
        .split_once('+')
        .map_or((value, None), |(a, b)| (a, Some(b)));
    let identifiers = |s: &str| {
        s.split('.').all(|part| {
            !part.is_empty() && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
    };
    if build.is_some_and(|b| !identifiers(b)) {
        return false;
    }
    let (core, pre) = base
        .split_once('-')
        .map_or((base, None), |(a, b)| (a, Some(b)));
    if pre.is_some_and(|p| {
        !identifiers(p)
            || p.split('.')
                .any(|p| p.bytes().all(|b| b.is_ascii_digit()) && !digits(p))
    }) {
        return false;
    }
    let parts: Vec<_> = core.split('.').collect();
    parts.len() == 3 && parts.iter().all(|v| digits(v))
}
fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn package_path(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 240
        || !value.as_bytes()[0].is_ascii_alphanumeric() && value.as_bytes()[0] != b'_'
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._/-".contains(&b))
    {
        return false;
    }
    value.split('/').all(|part| {
        let lower = part.to_ascii_lowercase();
        let stem = lower.split('.').next().unwrap_or("");
        !part.is_empty()
            && !part.starts_with('.')
            && !part.ends_with('.')
            && !matches!(
                stem,
                "con"
                    | "prn"
                    | "aux"
                    | "nul"
                    | "node_modules"
                    | "credentials"
                    | "secret"
                    | "secrets"
            )
            && !(stem.len() == 4
                && (stem.starts_with("com") || stem.starts_with("lpt"))
                && (b'1'..=b'9').contains(&stem.as_bytes()[3]))
            && ![".pem", ".key", ".p12", ".pfx"]
                .iter()
                .any(|suffix| lower.ends_with(suffix))
    })
}
fn validate_reference(value: &Value) -> Result<(), RunnerError> {
    exact(value, &["organization", "package", "version", "sha256"])?;
    if !slug(text(&value["organization"])?)
        || !slug(text(&value["package"])?)
        || !version(text(&value["version"])?)
        || !digest(text(&value["sha256"])?)
    {
        return Err(invalid());
    }
    Ok(())
}
fn reference_parts(value: &str) -> Result<(&str, &str, &str), RunnerError> {
    let (organization, rest) = value.split_once('/').ok_or_else(invalid)?;
    let (package, version_value) = rest.split_once('@').ok_or_else(invalid)?;
    if !slug(organization) || !slug(package) || !version(version_value) {
        return Err(invalid());
    }
    Ok((organization, package, version_value))
}
fn reference_matches(receipt: &Value, organization: &str, package: &str, version: &str) -> bool {
    receipt["reference"]["organization"] == organization
        && receipt["reference"]["package"] == package
        && receipt["reference"]["version"] == version
}
fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for part in bytes.chunks(3) {
        let a = part[0];
        let b = *part.get(1).unwrap_or(&0);
        let c = *part.get(2).unwrap_or(&0);
        for index in [a >> 2, (a & 3) << 4 | b >> 4] {
            output.push(char::from(ALPHABET[index as usize]));
        }
        output.push(if part.len() > 1 {
            char::from(ALPHABET[((b & 15) << 2 | c >> 6) as usize])
        } else {
            '='
        });
        output.push(if part.len() > 2 {
            char::from(ALPHABET[(c & 63) as usize])
        } else {
            '='
        });
    }
    output
}
fn decode_base64(value: &str) -> Result<Vec<u8>, RunnerError> {
    if value.len() % 4 != 0 {
        return Err(invalid());
    }
    let mut output = Vec::new();
    for group in value.as_bytes().chunks(4) {
        let mut n = [0_u8; 4];
        for (index, b) in group.iter().enumerate() {
            n[index] = match b {
                b'A'..=b'Z' => b - b'A',
                b'a'..=b'z' => b - b'a' + 26,
                b'0'..=b'9' => b - b'0' + 52,
                b'+' => 62,
                b'/' => 63,
                b'=' if index >= 2 => 0,
                _ => return Err(invalid()),
            };
        }
        output.push(n[0] << 2 | n[1] >> 4);
        if group[2] != b'=' {
            output.push(n[1] << 4 | n[2] >> 2);
        }
        if group[3] != b'=' {
            output.push(n[2] << 6 | n[3]);
        }
    }
    if base64(&output) != value {
        return Err(invalid());
    }
    Ok(output)
}
// Sender-side directory manifests reject duplicate keys, unlike network JSON consumers.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct UniqueVisitor;
        impl<'de> Visitor<'de> for UniqueVisitor {
            type Value = Unique;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("JSON with unique object keys")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Unique, M::Error> {
                let mut values = serde_json::Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if values.contains_key(&key) {
                        return Err(de::Error::custom("duplicate object key"));
                    }
                    values.insert(key, map.next_value::<Unique>()?.0);
                }
                Ok(Unique(Value::Object(values)))
            }
            fn visit_seq<S: SeqAccess<'de>>(self, mut seq: S) -> Result<Unique, S::Error> {
                let mut values = Vec::new();
                while let Some(v) = seq.next_element::<Unique>()? {
                    values.push(v.0);
                }
                Ok(Unique(Value::Array(values)))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
        }
        deserializer.deserialize_any(UniqueVisitor)
    }
}
#[derive(Debug)]
struct Prepared {
    bytes: Vec<u8>,
    inventory: Value,
    reference: Value,
    visibility: String,
    files: Vec<(String, Vec<u8>)>,
}
fn prepare(value: &Value) -> Result<Prepared, RunnerError> {
    exact(value, &["schema", "manifest", "files"])?;
    if value["schema"] != "prose-package-v1"
        || serde_json::to_vec(value).map_err(|_| invalid())?.len() > LIMIT
    {
        return Err(invalid());
    }
    let manifest = &value["manifest"];
    fields(
        manifest,
        &[
            "organization",
            "package",
            "version",
            "exports",
            "dependencies",
            "visibility",
        ],
        &[
            "organization",
            "package",
            "version",
            "exports",
            "dependencies",
        ],
    )?;
    if !slug(text(&manifest["organization"])?)
        || !slug(text(&manifest["package"])?)
        || !version(text(&manifest["version"])?)
    {
        return Err(invalid());
    }
    let visibility = manifest
        .get("visibility")
        .map(text)
        .transpose()?
        .unwrap_or("private");
    if !matches!(visibility, "public" | "private") {
        return Err(invalid());
    }
    let exports = manifest["exports"].as_object().ok_or_else(invalid)?;
    let dependencies = manifest["dependencies"].as_object().ok_or_else(invalid)?;
    if exports.is_empty() || exports.len() > 64 || dependencies.len() > 64 {
        return Err(invalid());
    }
    for (alias, reference) in dependencies {
        if !slug(alias) {
            return Err(invalid());
        }
        validate_reference(reference)?;
    }
    let input_files = value["files"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 128)
        .ok_or_else(invalid)?;
    let mut files = BTreeMap::new();
    let mut folded = BTreeSet::new();
    let mut total = 0;
    for file in input_files {
        exact(file, &["path", "encoding", "content"])?;
        let path = text(&file["path"])?;
        let content = text(&file["content"])?;
        if !package_path(path) || content.len() > LIMIT || !folded.insert(path.to_ascii_lowercase())
        {
            return Err(invalid());
        }
        let bytes = match text(&file["encoding"])? {
            "utf8" => content.as_bytes().to_vec(),
            "base64" => decode_base64(content)?,
            _ => return Err(invalid()),
        };
        total += bytes.len();
        if bytes.len() > FILE_LIMIT || total > TOTAL_LIMIT {
            return Err(invalid());
        }
        files.insert(path.to_owned(), bytes);
    }
    for path in &folded {
        if folded
            .iter()
            .any(|other| other.starts_with(&format!("{path}/")))
        {
            return Err(invalid());
        }
    }
    for (name, path) in exports {
        let name_ok = !name.is_empty()
            && name.len() <= 64
            && name.as_bytes()[0].is_ascii_alphabetic()
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            && !matches!(name.as_str(), "prototype" | "constructor" | "__proto__");
        if !name_ok || !files.contains_key(text(path)?) {
            return Err(invalid());
        }
    }
    let mut manifest = manifest.clone();
    manifest["visibility"] = json!(visibility);
    let inventory: Vec<Value> = files
        .iter()
        .map(|(path, bytes)| json!({"path":path,"size":bytes.len(),"sha256":sha(bytes)}))
        .collect();
    let artifact = json!({"schema":"prose-package-v1","manifest":manifest,"files":files.iter().map(|(path,bytes)|json!({"path":path,"encoding":"base64","content":base64(bytes)})).collect::<Vec<_>>()});
    let mut bytes = serde_json::to_vec(&artifact).map_err(|_| invalid())?;
    bytes.push(b'\n');
    if bytes.len() > LIMIT {
        return Err(invalid());
    }
    let reference = json!({"organization":manifest["organization"],"package":manifest["package"],"version":manifest["version"],"sha256":sha(&bytes)});
    Ok(Prepared {
        bytes,
        inventory: json!(inventory),
        reference,
        visibility: visibility.into(),
        files: files.into_iter().collect(),
    })
}
// JSON's integer-valued numeric spellings (8, 8.0, 8e0) denote the same size.
#[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
fn receipt_size(value: &Value) -> Option<u64> {
    let limit = f64::from(u32::try_from(FILE_LIMIT).ok()?);
    value
        .as_f64()
        .filter(|size| *size >= 0.0 && *size <= limit && size.fract() == 0.0)
        .map(|size| size as u64)
}
fn receipt(value: &Value) -> Result<(), RunnerError> {
    exact(
        value,
        &[
            "schema",
            "organizationId",
            "reference",
            "visibility",
            "inventory",
        ],
    )?;
    let uuid = text(&value["organizationId"])?;
    if uuid.len() != 36
        || !uuid.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
        || value["schema"] != "prose-publication-v1"
        || !matches!(text(&value["visibility"])?, "private" | "public")
    {
        return Err(invalid());
    }
    validate_reference(&value["reference"])?;
    let inventory = value["inventory"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 128)
        .ok_or_else(invalid)?;
    let mut previous = "";
    let mut folded = BTreeSet::new();
    let mut total = 0_u64;
    for file in inventory {
        exact(file, &["path", "size", "sha256"])?;
        let path = text(&file["path"])?;
        let size = receipt_size(&file["size"]).ok_or_else(invalid)?;
        total = total.checked_add(size).ok_or_else(invalid)?;
        if !package_path(path)
            || path <= previous
            || !folded.insert(path.to_ascii_lowercase())
            || size > FILE_LIMIT as u64
            || total > TOTAL_LIMIT as u64
            || !digest(text(&file["sha256"])?)
        {
            return Err(invalid());
        }
        previous = path;
    }
    for path in &folded {
        if folded
            .iter()
            .any(|other| other.starts_with(&format!("{path}/")))
        {
            return Err(invalid());
        }
    }
    Ok(())
}
fn agrees(value: &Value, prepared: &Prepared) -> bool {
    value["reference"] == prepared.reference
        && value["visibility"] == prepared.visibility
        && value["inventory"].as_array().is_some_and(|inventory| {
            let expected = prepared.inventory.as_array().expect("prepared inventory");
            inventory.len() == expected.len()
                && inventory.iter().zip(expected).all(|(left, right)| {
                    left["path"] == right["path"]
                        && left["sha256"] == right["sha256"]
                        && receipt_size(&left["size"]) == receipt_size(&right["size"])
                })
        })
}

/// Options per command, in help order (mirrors Bun `package-args.ts` OPTIONS).
fn options_of(operation: &str) -> &'static [&'static str] {
    match operation {
        "publish" => &["--organization", "--name", "--version", "--public"],
        "fetch" => &["--output-dir", "--sha256"],
        "list" => &["--cursor"],
        _ => &[],
    }
}
fn target_of(operation: &str) -> (&'static str, &'static str) {
    match operation {
        "publish" => ("FILE|DIR", "the file or package directory to publish"),
        "fetch" => ("ORG/NAME@VERSION", "the exact package version to download"),
        "list" => ("ORG", "the organization whose public packages to list"),
        _ => ("ORG/NAME@VERSION", "the exact package version to withdraw"),
    }
}
const SLUG_RULE: &str = "lowercase letters, digits and inner hyphens, at most 63 characters";
fn quoted(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_default()
}
fn joined(names: &[&str]) -> String {
    match names {
        [one] => (*one).to_owned(),
        [rest @ .., last] => format!("{} and {last}", rest.join(", ")),
        [] => String::new(),
    }
}
fn slug_problem(label: &str, value: &str) -> Option<String> {
    (!slug(value)).then(|| {
        format!(
            "{label} {} is not a valid slug ({SLUG_RULE})",
            quoted(value)
        )
    })
}
fn version_problem(value: &str) -> Option<String> {
    (!version(value)).then(|| {
        format!(
            "VERSION {} is not an exact semantic version (for example 1.2.0)",
            quoted(value)
        )
    })
}
/// Why an `ORG/NAME@VERSION` reference is invalid (mirrors Bun `referenceProblem`).
fn reference_problem(value: &str) -> Option<String> {
    let Some((organization, package, version_value)) =
        value.split_once('/').and_then(|(organization, rest)| {
            rest.split_once('@')
                .map(|(package, version_value)| (organization, package, version_value))
        })
    else {
        return Some(format!(
            "{} is not ORG/NAME@VERSION (for example acme/tool@1.2.0)",
            quoted(value)
        ));
    };
    slug_problem("ORG", organization)
        .or_else(|| slug_problem("NAME", package))
        .or_else(|| version_problem(version_value))
}
fn package_help_error(reason: String) -> RunnerError {
    let mut error = RunnerError::invocation(reason);
    error.action = crate::service::render::localize_product(
        "Run `prose cli package --help` to see the package commands.",
    );
    error
}
/// Parses `cli package <COMMAND> ...`. A missing or unknown command is a bare
/// `INVOCATION_INVALID`; any other problem is `PackageCommand::Invalid`, which
/// `execute` reports in the openprose.service-operation/1 envelope before any
/// request (mirrors Bun `parsePackageCommand`).
pub(crate) fn parse(args: &[String]) -> Result<(PackageCommand, bool), RunnerError> {
    let (args, json) = match args {
        [rest @ .., last] if last == "--json" => (rest, true),
        _ => (args, false),
    };
    let Some(operation) = args.first().filter(|value| !value.is_empty()) else {
        return Err(package_help_error(
            "cli package needs a command: publish, fetch, list or withdraw".to_owned(),
        ));
    };
    let operation: &'static str = match operation.as_str() {
        "publish" => "publish",
        "fetch" => "fetch",
        "list" => "list",
        "withdraw" => "withdraw",
        other => {
            return Err(package_help_error(format!(
                "unknown package command {}; the commands are publish, fetch, list and withdraw",
                quoted(other)
            )));
        }
    };
    let command = match parse_operation(operation, &args[1..]) {
        Ok(command) => command,
        Err(reason) => PackageCommand::Invalid { operation, reason },
    };
    Ok((command, json))
}
fn parse_operation(operation: &'static str, rest: &[String]) -> Result<PackageCommand, String> {
    let (target_name, target_what) = target_of(operation);
    let Some(target) = rest
        .first()
        .filter(|value| !value.is_empty() && !value.starts_with('-'))
    else {
        return Err(format!(
            "package {operation} needs {target_name}, {target_what}"
        ));
    };
    let allowed = options_of(operation);
    let mut values: BTreeMap<&str, String> = BTreeMap::new();
    let mut index = 1;
    while let Some(raw) = rest.get(index) {
        if raw == "--json" {
            return Err("--json must be the last argument".to_owned());
        }
        if !raw.starts_with("--") {
            return Err(format!(
                "unexpected argument {}; package {operation} takes one {target_name}",
                quoted(raw)
            ));
        }
        let (name, inline) = raw
            .split_once('=')
            .map_or((raw.as_str(), None), |(name, value)| (name, Some(value)));
        let Some(name) = allowed.iter().copied().find(|option| *option == name) else {
            return Err(format!(
                "package {operation} does not take {name}; {}",
                if allowed.is_empty() {
                    "it takes no options".to_owned()
                } else {
                    format!("its options are {}", joined(allowed))
                }
            ));
        };
        if values.contains_key(name) {
            return Err(format!("{name} was given twice"));
        }
        if name == "--public" {
            if inline.is_some() {
                return Err("--public takes no value".to_owned());
            }
            values.insert(name, String::new());
            index += 1;
            continue;
        }
        let value = match inline {
            Some(value) => Some(value),
            None => rest.get(index + 1).map(String::as_str),
        };
        match value {
            Some(value) if !value.is_empty() && (inline.is_some() || !value.starts_with('-')) => {
                values.insert(name, value.to_owned());
            }
            _ => return Err(format!("{name} needs a value")),
        }
        index += if inline.is_some() { 1 } else { 2 };
    }
    let required: &[(&str, &str)] = match operation {
        "publish" => &[
            ("--organization", "ORG"),
            ("--name", "NAME"),
            ("--version", "VERSION"),
        ],
        "fetch" => &[("--output-dir", "FRESH_DIR, a new directory to create")],
        _ => &[],
    };
    for (name, what) in required {
        if !values.contains_key(name) {
            return Err(format!("package {operation} needs {name} {what}"));
        }
    }
    let mut take = |name: &str| values.remove(name);
    let problem = |found: Option<String>| found.map_or(Ok(()), Err);
    Ok(match operation {
        "publish" => {
            let organization = take("--organization").unwrap_or_default();
            let name = take("--name").unwrap_or_default();
            let version_value = take("--version").unwrap_or_default();
            problem(
                slug_problem("ORG", &organization)
                    .or_else(|| slug_problem("NAME", &name))
                    .or_else(|| version_problem(&version_value)),
            )?;
            PackageCommand::Publish {
                source: target.clone(),
                organization,
                name,
                version: version_value,
                public: take("--public").is_some(),
            }
        }
        "fetch" => {
            let destination = take("--output-dir").unwrap_or_default();
            let sha256 = take("--sha256");
            problem(reference_problem(target).or_else(|| {
                sha256
                    .as_deref()
                    .is_some_and(|value| !digest(value))
                    .then(|| "--sha256 must be 64 lowercase hexadecimal digits".to_owned())
            }))?;
            PackageCommand::Fetch {
                reference: target.clone(),
                destination,
                sha256,
            }
        }
        "list" => {
            let cursor = take("--cursor");
            problem(slug_problem("ORG", target).or_else(|| {
                cursor.as_deref().filter(|value| !valid_cursor(value)).map(|value| {
                    format!(
                        "--cursor {} is not a cursor from package list; pass the nextCursor value the previous page printed",
                        quoted(value)
                    )
                })
            }))?;
            PackageCommand::List {
                organization: target.clone(),
                cursor,
            }
        }
        _ => {
            problem(reference_problem(target))?;
            PackageCommand::Withdraw {
                reference: target.clone(),
            }
        }
    })
}
fn valid_cursor(value: &str) -> bool {
    if value.len() > 210 {
        return false;
    }
    let Some(rest) = value.strip_prefix("public:") else {
        return false;
    };
    let Some((package, version_value)) = rest.split_once(':') else {
        return false;
    };
    !package.is_empty()
        && package
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !version_value.is_empty()
        && version_value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+-".contains(&b))
}
fn encode_query(value: &str) -> String {
    value
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                char::from(b).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
fn absolute(path: &Path, cwd: &Path) -> Result<PathBuf, RunnerError> {
    let value = if path.is_absolute() {
        path.to_owned()
    } else {
        cwd.join(path)
    };
    if value
        .components()
        .any(|c| matches!(c, Component::ParentDir | Component::Prefix(_)))
    {
        return Err(invalid());
    }
    Ok(value)
}
#[cfg(unix)]
fn open_safe(path: &Path, directory: bool) -> Result<File, RunnerError> {
    use rustix::fs::{CWD, Mode, OFlags, openat};
    let mut file = File::from(
        openat(
            CWD,
            "/",
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| invalid())?,
    );
    let parts: Vec<_> = path
        .components()
        .filter_map(|c| match c {
            Component::Normal(v) => Some(v),
            _ => None,
        })
        .collect();
    for (i, part) in parts.iter().enumerate() {
        let is_dir = i + 1 < parts.len() || directory;
        let flags = OFlags::RDONLY
            | OFlags::NOFOLLOW
            | OFlags::CLOEXEC
            | OFlags::NONBLOCK
            | if is_dir {
                OFlags::DIRECTORY
            } else {
                OFlags::empty()
            };
        file = File::from(openat(&file, *part, flags, Mode::empty()).map_err(|_| invalid())?);
    }
    Ok(file)
}
#[cfg(not(unix))]
fn open_safe(_: &Path, _: bool) -> Result<File, RunnerError> {
    Err(RunnerError::config(
        "Safe package filesystem access is unavailable on this platform.",
    ))
}
fn bounded_file(path: &Path, limit: usize) -> Result<Vec<u8>, RunnerError> {
    let file = open_safe(path, false)?;
    let before = file.metadata().map_err(|_| invalid())?;
    if !before.is_file() || before.len() > limit as u64 {
        return Err(invalid());
    }
    let mut bytes = Vec::new();
    (&file)
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    let after = file.metadata().map_err(|_| invalid())?;
    if bytes.len() > limit
        || before.len() != after.len()
        || after.len() != bytes.len() as u64
        || before.modified().ok() != after.modified().ok()
    {
        return Err(invalid());
    }
    Ok(bytes)
}
fn source_package(
    source: &str,
    organization: &str,
    name: &str,
    version: &str,
    public: bool,
    cwd: &Path,
) -> Result<Prepared, RunnerError> {
    let source = absolute(Path::new(source), cwd)?;
    let meta = open_safe(&source, false)?
        .metadata()
        .map_err(|_| invalid())?;
    let mut files = Vec::new();
    let (exports, dependencies) = if meta.is_dir() {
        let bytes = bounded_file(&source.join("prose-package.json"), LIMIT)?;
        let manifest = serde_json::from_slice::<Unique>(&bytes)
            .map_err(|_| invalid())?
            .0;
        exact(&manifest, &["schema", "files", "exports", "dependencies"])?;
        if manifest["schema"] != "prose-package-directory-v1" {
            return Err(invalid());
        }
        let paths = manifest["files"]
            .as_array()
            .filter(|v| !v.is_empty() && v.len() <= 128)
            .ok_or_else(invalid)?;
        let mut total = 0;
        for path in paths {
            let path = text(path)?;
            if !package_path(path) {
                return Err(invalid());
            }
            let bytes = bounded_file(&source.join(path), FILE_LIMIT)?;
            total += bytes.len();
            if total > TOTAL_LIMIT {
                return Err(invalid());
            }
            files.push(json!({"path":path,"encoding":"base64","content":base64(&bytes)}));
        }
        (
            manifest["exports"].clone(),
            manifest["dependencies"].clone(),
        )
    } else if meta.is_file() {
        let name = source
            .file_name()
            .and_then(|v| v.to_str())
            .filter(|v| package_path(v))
            .ok_or_else(invalid)?;
        let bytes = bounded_file(&source, FILE_LIMIT)?;
        files.push(json!({"path":name,"encoding":"base64","content":base64(&bytes)}));
        (json!({"default":name}), json!({}))
    } else {
        return Err(invalid());
    };
    prepare(
        &json!({"schema":"prose-package-v1","manifest":{"organization":organization,"package":name,"version":version,"visibility":if public{"public"}else{"private"},"exports":exports,"dependencies":dependencies},"files":files}),
    )
}

#[cfg(any(target_os = "linux", target_os = "android", target_vendor = "apple"))]
fn materialize(
    destination: &Path,
    prepared: &Prepared,
    receipt: &Value,
) -> Result<(), RunnerError> {
    use rustix::fs::{AtFlags, Mode, OFlags, RenameFlags, mkdirat, openat, renameat_with};
    use std::os::unix::fs::MetadataExt as _;
    let parent = destination.parent().ok_or_else(invalid)?;
    let name = destination.file_name().ok_or_else(invalid)?;
    let parent_fd = open_safe(parent, true)?;
    let temporary = format!(".prose-package-{}", uuid::Uuid::now_v7());
    mkdirat(
        &parent_fd,
        temporary.as_str(),
        Mode::RUSR | Mode::WUSR | Mode::XUSR,
    )
    .map_err(|_| invalid())?;
    let temp = File::from(
        openat(
            &parent_fd,
            temporary.as_str(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| invalid())?,
    );
    let mut written: Vec<(File, String, rustix::fs::Stat)> = Vec::new();
    let mut created_directories: Vec<(File, String, rustix::fs::Stat)> = Vec::new();
    let temp_identity = rustix::fs::fstat(&temp).map_err(|_| invalid())?;
    let mut directories = BTreeSet::new();
    let result = (|| {
        let mut all = prepared.files.clone();
        all.push((
            ".prose-package-receipt.json".into(),
            serde_json::to_vec(receipt).map_err(|_| invalid())?,
        ));
        for (path, bytes) in all {
            let mut current = temp.try_clone().map_err(|_| invalid())?;
            let components: Vec<_> = path.split('/').collect();
            let mut prefix = String::new();
            for part in &components[..components.len() - 1] {
                if !prefix.is_empty() {
                    prefix.push('/');
                }
                prefix.push_str(part);
                if directories.insert(prefix.clone()) {
                    let owner = current.try_clone().map_err(|_| invalid())?;
                    mkdirat(&current, *part, Mode::RUSR | Mode::WUSR | Mode::XUSR)
                        .map_err(|_| invalid())?;
                    let identity = rustix::fs::statat(&owner, *part, AtFlags::SYMLINK_NOFOLLOW)
                        .map_err(|_| invalid())?;
                    created_directories.push((owner, (*part).to_owned(), identity));
                }
                current = File::from(
                    openat(
                        &current,
                        *part,
                        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                        Mode::empty(),
                    )
                    .map_err(|_| invalid())?,
                );
            }
            let mut output = File::from(
                openat(
                    &current,
                    *components.last().ok_or_else(invalid)?,
                    OFlags::WRONLY
                        | OFlags::CREATE
                        | OFlags::EXCL
                        | OFlags::NOFOLLOW
                        | OFlags::CLOEXEC,
                    Mode::RUSR | Mode::WUSR,
                )
                .map_err(|_| invalid())?,
            );
            written.push((
                current,
                (*components.last().ok_or_else(invalid)?).to_owned(),
                rustix::fs::fstat(&output).map_err(|_| invalid())?,
            ));
            output.write_all(&bytes).map_err(|_| invalid())?;
            output.sync_all().map_err(|_| invalid())?;
        }
        // Reauthenticate the private directory name immediately before publication.
        let named = File::from(
            openat(
                &parent_fd,
                temporary.as_str(),
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .map_err(|_| invalid())?,
        );
        let expected = temp.metadata().map_err(|_| invalid())?;
        let observed = named.metadata().map_err(|_| invalid())?;
        if (expected.dev(), expected.ino()) != (observed.dev(), observed.ino()) {
            return Err(invalid());
        }
        // NOREPLACE is the publication boundary: a concurrent target is never replaced.
        renameat_with(
            &parent_fd,
            temporary.as_str(),
            &parent_fd,
            name,
            RenameFlags::NOREPLACE,
        )
        .map_err(|_| invalid())?;
        Ok(())
    })();
    if result.is_err() {
        for (parent, name, identity) in written.iter().rev() {
            remove_owned_entry(parent, name, identity, AtFlags::empty());
        }
        for (parent, name, identity) in created_directories.iter().rev() {
            remove_owned_entry(parent, name, identity, AtFlags::REMOVEDIR);
        }
        remove_owned_entry(
            &parent_fd,
            temporary.as_str(),
            &temp_identity,
            AtFlags::REMOVEDIR,
        );
    }
    result
}
#[cfg(any(target_os = "linux", target_os = "android", target_vendor = "apple"))]
fn remove_owned_entry(
    parent: &File,
    name: &str,
    identity: &rustix::fs::Stat,
    flags: rustix::fs::AtFlags,
) {
    use rustix::fs::{AtFlags, statat, unlinkat};
    if statat(parent, name, AtFlags::SYMLINK_NOFOLLOW).is_ok_and(|current| {
        current.st_dev == identity.st_dev
            && current.st_ino == identity.st_ino
            && current.st_mode == identity.st_mode
    }) {
        let _ = unlinkat(parent, name, flags);
    }
}

#[cfg(not(any(target_os = "linux", target_os = "android", target_vendor = "apple")))]
fn materialize(_: &Path, _: &Prepared, _: &Value) -> Result<(), RunnerError> {
    Err(RunnerError::config(
        "Atomic package materialization is unavailable on this platform.",
    ))
}

pub(crate) fn execute(
    command: &PackageCommand,
    environment: &Environment,
    cwd: &Path,
    mode: OutputMode,
    cancellation: &CancellationToken,
) -> CommandOutcome {
    let operation = match command {
        PackageCommand::Publish { .. } => "publish",
        PackageCommand::Fetch { .. } => "fetch",
        PackageCommand::List { .. } => "list",
        PackageCommand::Withdraw { .. } => "withdraw",
        PackageCommand::Invalid { operation, .. } => operation,
    };
    let result = (|| -> Result<Value, RunnerError> {
        if let PackageCommand::Invalid { operation, reason } = command {
            return Err(crate::service::teach(
                RunnerError::invocation(reason.clone()),
                &crate::service::Correction::Command {
                    action:
                        "Correct the value named in Detail; `{command}` shows the accepted syntax"
                            .to_owned(),
                    words: vec![
                        "package".to_owned(),
                        (*operation).to_owned(),
                        "--help".to_owned(),
                    ],
                },
                mode,
            ));
        }
        // Validate and collect local publication bytes before accessing credentials.
        let prepared = if let PackageCommand::Publish {
            source,
            organization,
            name,
            version,
            public,
        } = command
        {
            Some(source_package(
                source,
                organization,
                name,
                version,
                *public,
                cwd,
            )?)
        } else {
            None
        };
        let destination = if let PackageCommand::Fetch { destination, .. } = command {
            let path = absolute(Path::new(destination), cwd)?;
            open_safe(path.parent().ok_or_else(invalid)?, true)?;
            match std::fs::symlink_metadata(&path) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                _ => return Err(invalid()),
            }
            Some(path)
        } else {
            None
        };
        let mut session = Session::registry(cancellation, environment.clone())?;
        let required = matches!(
            command,
            PackageCommand::Publish { .. } | PackageCommand::Withdraw { .. }
        );
        let token = session.registry_credential(required)?;
        let output: Value = (match command {
            PackageCommand::Publish {
                organization, name, ..
            } => {
                let prepared = prepared.ok_or_else(invalid)?;
                let path =
                    format!("/registry/v1/organizations/{organization}/packages/{name}/versions");
                let bytes = session.registry_request(
                    "POST",
                    &path,
                    token.as_deref(),
                    Some(&prepared.bytes),
                    &[200, 201],
                )?;
                let value: Value = crate::service::http::parse_json(&bytes).ok_or_else(protocol)?;
                receipt(&value).map_err(|_| protocol())?;
                if !agrees(&value, &prepared) {
                    return Err(protocol());
                }
                Ok(value)
            }
            PackageCommand::Fetch {
                reference, sha256, ..
            } => {
                let (organization, package, version) = reference_parts(reference)?;
                let path = format!(
                    "/registry/v1/organizations/{organization}/packages/{package}/versions/{version}"
                );
                let bytes =
                    session.registry_request("GET", &path, token.as_deref(), None, &[200])?;
                let value: Value = crate::service::http::parse_json(&bytes).ok_or_else(protocol)?;
                receipt(&value).map_err(|_| protocol())?;
                if !reference_matches(&value, organization, package, version)
                    || sha256
                        .as_ref()
                        .is_some_and(|hash| value["reference"]["sha256"] != *hash)
                {
                    return Err(protocol());
                }
                let bytes = session.registry_request(
                    "GET",
                    &format!("{path}/artifact"),
                    token.as_deref(),
                    None,
                    &[200],
                )?;
                if value["reference"]["sha256"] != sha(&bytes) {
                    return Err(protocol());
                }
                let artifact: Value =
                    crate::service::http::parse_json(&bytes).ok_or_else(protocol)?;
                let prepared = prepare(&artifact).map_err(|_| protocol())?;
                if prepared.bytes != bytes || !agrees(&value, &prepared) {
                    return Err(protocol());
                }
                if token
                    .as_ref()
                    .is_some_and(|token| value.to_string().contains(token))
                {
                    return Err(protocol());
                }
                cancellation_check(cancellation)?;
                materialize(&destination.ok_or_else(invalid)?, &prepared, &value)?;
                Ok(value)
            }
            PackageCommand::List {
                organization,
                cursor,
            } => {
                let path = format!(
                    "/registry/v1/organizations/{organization}/packages{}",
                    cursor
                        .as_ref()
                        .map_or(String::new(), |v| format!("?cursor={}", encode_query(v)))
                );
                let bytes =
                    session.registry_request("GET", &path, token.as_deref(), None, &[200])?;
                let value: Value = crate::service::http::parse_json(&bytes).ok_or_else(protocol)?;
                exact(&value, &["packages", "nextCursor"]).map_err(|_| protocol())?;
                let packages = value["packages"]
                    .as_array()
                    .filter(|v| v.len() <= 25)
                    .ok_or_else(protocol)?;
                for item in packages {
                    receipt(item).map_err(|_| protocol())?;
                    if item["visibility"] != "public"
                        || item["reference"]["organization"] != *organization
                    {
                        return Err(protocol());
                    }
                }
                if !value["nextCursor"].is_null()
                    && !value["nextCursor"].as_str().is_some_and(valid_cursor)
                {
                    return Err(protocol());
                }
                Ok(value)
            }
            PackageCommand::Withdraw { reference } => {
                let (organization, package, version) = reference_parts(reference)?;
                let path = format!(
                    "/registry/v1/organizations/{organization}/packages/{package}/versions/{version}/withdraw"
                );
                let bytes = session.registry_request(
                    "POST",
                    &path,
                    token.as_deref(),
                    Some(b"{}"),
                    &[200],
                )?;
                let value: Value = crate::service::http::parse_json(&bytes).ok_or_else(protocol)?;
                exact(&value, &["receipt", "withdrawn"]).map_err(|_| protocol())?;
                receipt(&value["receipt"]).map_err(|_| protocol())?;
                if value["withdrawn"] != true
                    || !reference_matches(&value["receipt"], organization, package, version)
                {
                    return Err(protocol());
                }
                Ok(value)
            }
            PackageCommand::Invalid { .. } => Err(invalid()),
        })?;
        if token
            .as_ref()
            .is_some_and(|token| output.to_string().contains(token))
        {
            return Err(protocol());
        }
        Ok(output)
    })();
    let (value, error) = match result {
        Ok(value) => (value, None),
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => {
            let (kind, id, reason, list) = missing(command);
            let words = list.iter().map(String::as_str).collect::<Vec<_>>();
            let error = crate::service::not_found::explain_not_found(
                error.with_detail("reason", reason),
                mode,
                kind,
                &id,
                Some(&words),
            );
            (Value::Null, Some(error))
        }
        Err(error) => (Value::Null, Some(error)),
    };
    let exit = error.as_ref().map_or(0, |e| e.exit_code);
    if mode == OutputMode::Human {
        let marker = if environment.is_custom() {
            format!("{}\n", environment.label())
        } else {
            String::new()
        };
        if let Some(error) = error {
            // A failure's first line names the service.
            CommandOutcome::human(
                "",
                crate::service::render::human_error(&environment.label(), &error),
                exit,
            )
        } else {
            let format_reference = |receipt: &Value| {
                format!(
                    "{}/{}@{}",
                    receipt["reference"]["organization"].as_str().unwrap_or(""),
                    receipt["reference"]["package"].as_str().unwrap_or(""),
                    receipt["reference"]["version"].as_str().unwrap_or("")
                )
            };
            let stdout = if operation == "list" {
                let mut text = String::new();
                if value["packages"].as_array().is_some_and(Vec::is_empty) {
                    if let PackageCommand::List { organization, .. } = command {
                        text.push_str("No public packages in ");
                        text.push_str(organization);
                        text.push_str(".\n");
                    }
                }
                for receipt in value["packages"].as_array().unwrap() {
                    text.push_str(&format_reference(receipt));
                    text.push('\n');
                }
                if let Some(cursor) = value["nextCursor"].as_str() {
                    text.push_str("Next cursor: ");
                    text.push_str(cursor);
                    text.push('\n');
                }
                text
            } else {
                let receipt = if operation == "withdraw" {
                    &value["receipt"]
                } else {
                    &value
                };
                format!(
                    "OpenProse package {operation}: {}\n",
                    format_reference(receipt)
                )
            };
            CommandOutcome::human(stdout, "", 0).with_preamble(marker)
        }
    } else {
        // The service-operation/1 envelope every `cli` command prints.
        let manifest_operation = crate::service::operation(&format!("package.{operation}"))
            .expect("package operations are in the manifest");
        let result = match error {
            Some(error) => Err(error),
            None => Ok(value),
        };
        crate::service::render::outcome(manifest_operation, environment, mode, result, None)
    }
}
/// What a registry 404 names (mirrors Bun `missing`): the version, or the
/// organization of a listing or publication.
fn missing(command: &PackageCommand) -> (&'static str, String, String, Vec<String>) {
    match command {
        PackageCommand::Fetch { reference, .. } | PackageCommand::Withdraw { reference } => {
            let organization = reference.split('/').next().unwrap_or_default();
            (
                "package",
                reference.clone(),
                format!(
                    "package {reference} was not found; the version does not exist, or this key cannot read it"
                ),
                vec![
                    "package".to_owned(),
                    "list".to_owned(),
                    organization.to_owned(),
                ],
            )
        }
        PackageCommand::List { organization, .. }
        | PackageCommand::Publish { organization, .. } => (
            "organization",
            organization.clone(),
            format!("organization {organization} was not found"),
            vec!["org".to_owned(), "list".to_owned()],
        ),
        PackageCommand::Invalid { .. } => (
            "package",
            String::new(),
            String::new(),
            vec!["package".to_owned(), "--help".to_owned()],
        ),
    }
}
fn cancellation_check(cancellation: &CancellationToken) -> Result<(), RunnerError> {
    if cancellation.is_cancelled() {
        Err(RunnerError::catalog(ErrorCode::Cancelled))
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture_bytes(name: &str) -> &'static [u8] {
        match name {
            "hash-vectors.json" => {
                include_bytes!("../../../../shared/fixtures/registry/hash-vectors.json")
            }
            "single-file.json" => {
                include_bytes!("../../../../shared/fixtures/registry/single-file.json")
            }
            "single-file.canonical.json" => {
                include_bytes!("../../../../shared/fixtures/registry/single-file.canonical.json")
            }
            "single-file.receipt.json" => {
                include_bytes!("../../../../shared/fixtures/registry/single-file.receipt.json")
            }
            "directory.json" => {
                include_bytes!("../../../../shared/fixtures/registry/directory.json")
            }
            "directory.canonical.json" => {
                include_bytes!("../../../../shared/fixtures/registry/directory.canonical.json")
            }
            "directory.receipt.json" => {
                include_bytes!("../../../../shared/fixtures/registry/directory.receipt.json")
            }
            _ => panic!("unknown registry fixture"),
        }
    }
    fn fixture(name: &str) -> Value {
        serde_json::from_slice(fixture_bytes(name)).unwrap()
    }
    #[cfg(any(target_os = "linux", target_os = "android", target_vendor = "apple"))]
    #[test]
    fn cleanup_leaves_replacement_files_directories_and_symlinks_untouched() {
        use rustix::fs::{AtFlags, statat};
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().unwrap();
        let cwd = std::fs::canonicalize(root.path()).unwrap();
        let parent = open_safe(&cwd, true).unwrap();
        std::fs::write(cwd.join("owned"), b"owned").unwrap();
        let identity = statat(&parent, "owned", AtFlags::SYMLINK_NOFOLLOW).unwrap();
        std::fs::rename(cwd.join("owned"), cwd.join("retained-original")).unwrap();
        std::fs::write(cwd.join("owned"), b"replacement").unwrap();
        remove_owned_entry(&parent, "owned", &identity, AtFlags::empty());
        assert_eq!(std::fs::read(cwd.join("owned")).unwrap(), b"replacement");
        std::fs::remove_file(cwd.join("owned")).unwrap();
        symlink(cwd.join("retained-original"), cwd.join("owned")).unwrap();
        remove_owned_entry(&parent, "owned", &identity, AtFlags::empty());
        assert!(
            std::fs::symlink_metadata(cwd.join("owned"))
                .unwrap()
                .file_type()
                .is_symlink()
        );
        std::fs::create_dir(cwd.join("directory")).unwrap();
        let identity = statat(&parent, "directory", AtFlags::SYMLINK_NOFOLLOW).unwrap();
        std::fs::rename(cwd.join("directory"), cwd.join("retained-directory")).unwrap();
        std::fs::create_dir(cwd.join("directory")).unwrap();
        remove_owned_entry(&parent, "directory", &identity, AtFlags::REMOVEDIR);
        assert!(cwd.join("directory").is_dir());
    }

    #[test]
    fn receipt_integer_spelling_matches_javascript_contract() {
        let prepared = prepare(&fixture("single-file.json")).unwrap();
        let mut value = fixture("single-file.receipt.json");
        value["inventory"][0]["size"] = serde_json::from_str("8.0").unwrap();
        receipt(&value).unwrap();
        assert!(agrees(&value, &prepared));
        value["inventory"][0]["size"] = json!(8.5);
        assert!(receipt(&value).is_err());
    }
    #[test]
    fn normative_canonical_bytes_hashes_inventory_and_receipts() {
        for vector in fixture("hash-vectors.json").as_array().unwrap() {
            let prepared = prepare(&fixture(vector["fixture"].as_str().unwrap())).unwrap();
            assert_eq!(
                prepared.bytes,
                fixture_bytes(vector["canonical"].as_str().unwrap())
            );
            assert_eq!(
                prepared.bytes.len() as u64,
                vector["artifactBytes"].as_u64().unwrap()
            );
            assert_eq!(prepared.reference["sha256"], vector["sha256"]);
            assert_eq!(prepared.inventory, vector["inventory"]);
            let receipt_value = fixture(
                &vector["fixture"]
                    .as_str()
                    .unwrap()
                    .replace(".json", ".receipt.json"),
            );
            receipt(&receipt_value).unwrap();
            assert!(agrees(&receipt_value, &prepared));
        }
    }
    #[test]
    fn rejects_hostile_paths_fields_collisions_content_and_unpinned_dependencies() {
        for path in [
            "../a",
            "a/../b",
            "a/.git/x",
            "a\\b",
            "/a",
            "a/",
            "a//b",
            "a.",
            "con.md",
            "a/NUL.txt",
            "secrets.json",
            "a.key",
            "node_modules/a",
            "a b",
            "é.md",
        ] {
            let mut value = fixture("single-file.json");
            value["files"][0]["path"] = json!(path);
            value["manifest"]["exports"]["default"] = json!(path);
            assert!(prepare(&value).is_err(), "{path}");
        }
        for (key, value) in [
            ("encoding", json!("zip")),
            ("content", json!(null)),
            ("unknown", json!(true)),
        ] {
            let mut source = fixture("single-file.json");
            source["files"][0][key] = value;
            assert!(prepare(&source).is_err());
        }
        for content in ["Zg=", "Zh==", "Zg==AAAA", "Zg===", "????"] {
            assert!(decode_base64(content).is_err(), "{content}");
        }
        for paths in [["a", "A"], ["a", "a/b"]] {
            let mut source = fixture("single-file.json");
            source["files"] = json!([{"path":paths[0],"encoding":"utf8","content":""},{"path":paths[1],"encoding":"utf8","content":""}]);
            source["manifest"]["exports"] = json!({"default":paths[0]});
            assert!(prepare(&source).is_err());
        }
        let mut value = fixture("directory.json");
        value["manifest"]["dependencies"]["hello"]["version"] = json!("^1.0.0");
        assert!(prepare(&value).is_err());
        let mut value = fixture("single-file.receipt.json");
        value["inventory"][0]["size"] = json!(FILE_LIMIT + 1);
        assert!(receipt(&value).is_err());
    }
    #[test]
    fn canonical_base64_preserves_binary_and_semver_is_exact() {
        let bytes: Vec<_> = (0..=255).collect();
        assert_eq!(decode_base64(&base64(&bytes)).unwrap(), bytes);
        for value in ["0.0.0", "1.0.0+build", "1.2.3-alpha-1.0+build.01"] {
            assert!(version(value), "{value}");
        }
        for value in [
            "1.0",
            "01.0.0",
            "1.0.0-01",
            "1.0.0+",
            "1.0.0+foo+bar",
            "1.0.0\n",
            "^1.0.0",
        ] {
            assert!(!version(value), "{value}");
        }
    }
    #[test]
    fn local_directory_manifest_is_explicit_and_rejects_duplicate_keys() {
        let root = tempfile::tempdir().unwrap();
        let cwd = std::fs::canonicalize(root.path()).unwrap();
        std::fs::write(cwd.join("hello.md"), b"# Hello\n").unwrap();
        std::fs::write(cwd.join("prose-package.json"),r#"{"schema":"prose-package-directory-v1","files":["hello.md"],"exports":{"default":"hello.md"},"dependencies":{}}"#).unwrap();
        std::fs::write(cwd.join("not-included.secret"), b"ignore").unwrap();
        let result = source_package(".", "example", "hello", "1.0.0", false, &cwd).unwrap();
        assert_eq!(
            result.reference["sha256"],
            "fac5e538f24818de99c563a5d8c547a5fae1fbb7047e68dbe3cca9ef732ec61e"
        );
        std::fs::write(cwd.join("prose-package.json"),r#"{"schema":"prose-package-directory-v1","files":["hello.md"],"exports":{"default":"hello.md","default":"hello.md"},"dependencies":{}}"#).unwrap();
        assert!(source_package(".", "example", "hello", "1.0.0", false, &cwd).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn rejects_source_and_ancestor_symlinks_and_nonregular_files() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().unwrap();
        let cwd = std::fs::canonicalize(root.path()).unwrap();
        std::fs::create_dir(cwd.join("real")).unwrap();
        std::fs::write(cwd.join("real/hello.md"), b"hi").unwrap();
        symlink(cwd.join("real/hello.md"), cwd.join("link.md")).unwrap();
        symlink(cwd.join("real"), cwd.join("linked")).unwrap();
        assert!(source_package("link.md", "example", "hello", "1.0.0", false, &cwd).is_err());
        assert!(
            source_package("linked/hello.md", "example", "hello", "1.0.0", false, &cwd).is_err()
        );
    }
    #[cfg(any(target_os = "linux", target_os = "android", target_vendor = "apple"))]
    #[test]
    fn materialization_is_exact_and_never_replaces_existing_directory() {
        let root = tempfile::tempdir().unwrap();
        let cwd = std::fs::canonicalize(root.path()).unwrap();
        let prepared = prepare(&fixture("directory.json")).unwrap();
        let receipt = fixture("directory.receipt.json");
        let target = cwd.join("fetched");
        materialize(&target, &prepared, &receipt).unwrap();
        for (path, bytes) in &prepared.files {
            assert_eq!(std::fs::read(target.join(path)).unwrap(), *bytes);
        }
        assert_eq!(
            serde_json::from_slice::<Value>(
                &std::fs::read(target.join(".prose-package-receipt.json")).unwrap()
            )
            .unwrap(),
            receipt
        );
        assert!(materialize(&target, &prepared, &receipt).is_err());
        let empty = cwd.join("empty");
        std::fs::create_dir(&empty).unwrap();
        assert!(materialize(&empty, &prepared, &receipt).is_err());
        assert!(std::fs::read_dir(empty).unwrap().next().is_none());
        assert!(std::fs::read_dir(&cwd).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".prose-package-")
        }));
    }
}
