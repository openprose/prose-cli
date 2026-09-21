use crate::error::RunnerError;
use crate::invocation::{GlobalFlags, OutputMode};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    MacOs,
    Unix,
    Windows,
}

impl Platform {
    #[must_use]
    pub const fn current() -> Self {
        if cfg!(target_os = "macos") {
            Self::MacOs
        } else if cfg!(windows) {
            Self::Windows
        } else {
            Self::Unix
        }
    }
}

/// All ambient inputs used by configuration discovery. Tests construct this
/// value directly so no undocumented production environment variable is
/// needed to redirect user roots.
#[derive(Debug, Clone)]
pub struct SystemContext {
    pub current_dir: PathBuf,
    pub home_dir: Option<PathBuf>,
    pub xdg_config_home: Option<PathBuf>,
    pub appdata: Option<PathBuf>,
    pub environment: BTreeMap<String, String>,
    pub platform: Platform,
}

impl SystemContext {
    /// Captures the production process inputs used for discovery.
    ///
    /// # Errors
    ///
    /// Returns `CONFIG_INVALID` if the current directory is unavailable.
    pub fn capture() -> Result<Self, RunnerError> {
        let current_dir = env::current_dir().map_err(|error| {
            RunnerError::config(format!("cannot read the current directory: {error}"))
        })?;
        let environment: BTreeMap<String, String> = env::vars().collect();
        Ok(Self {
            current_dir,
            home_dir: env::var_os("HOME").map(PathBuf::from),
            xdg_config_home: env::var_os("XDG_CONFIG_HOME").map(PathBuf::from),
            appdata: env::var_os("APPDATA").map(PathBuf::from),
            environment,
            platform: Platform::current(),
        })
    }

    /// Resolves a user configuration path only from a non-empty absolute root.
    ///
    /// # Errors
    ///
    /// Returns `CONFIG_INVALID` rather than allowing an absent, empty, or
    /// relative ambient root to resolve inside the candidate workspace.
    pub fn user_config_path(&self) -> Result<PathBuf, RunnerError> {
        if let Some(root) = &self.xdg_config_home {
            return checked_config_root(root, "XDG_CONFIG_HOME")
                .map(|root| root.join("openprose").join("cli.toml"));
        }
        let (root, name) = match self.platform {
            Platform::MacOs | Platform::Unix => (self.home_dir.as_ref(), "HOME"),
            Platform::Windows => (self.appdata.as_ref(), "APPDATA"),
        };
        let root = root.ok_or_else(|| {
            RunnerError::config(format!(
                "{name} must be a non-empty absolute path to locate OpenProse user configuration."
            ))
        })?;
        let root = checked_config_root(root, name)?;
        Ok(match self.platform {
            Platform::MacOs => root
                .join("Library")
                .join("Application Support")
                .join("OpenProse")
                .join("cli.toml"),
            Platform::Unix => root.join(".config").join("openprose").join("cli.toml"),
            Platform::Windows => root.join("OpenProse").join("cli.toml"),
        })
    }
}

fn checked_config_root<'a>(root: &'a Path, name: &str) -> Result<&'a Path, RunnerError> {
    if root.as_os_str().is_empty() || !root.is_absolute() {
        return Err(RunnerError::config(format!(
            "{name} must be a non-empty absolute path to locate OpenProse user configuration."
        )));
    }
    Ok(root)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ConfigSourceKind {
    Flag,
    Environment,
    #[serde(rename = "project-config")]
    ProjectFile,
    #[serde(rename = "user-config")]
    UserFile,
    Default,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfigSource {
    pub kind: ConfigSourceKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location: Option<String>,
}

impl ConfigSource {
    fn new(kind: ConfigSourceKind, location: Option<String>) -> Self {
        Self { kind, location }
    }

    fn flag(name: &str) -> Self {
        Self::new(ConfigSourceKind::Flag, Some(name.to_owned()))
    }

    fn environment(name: &str) -> Self {
        Self::new(ConfigSourceKind::Environment, Some(name.to_owned()))
    }

    fn file(kind: ConfigSourceKind, path: &Path) -> Self {
        Self::new(kind, Some(path.display().to_string()))
    }

    fn default() -> Self {
        Self::new(ConfigSourceKind::Default, Some("built-in".to_owned()))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Sourced<T> {
    pub value: T,
    pub source: ConfigSource,
}

impl<T> Sourced<T> {
    fn replace(&mut self, value: T, source: ConfigSource) {
        self.value = value;
        self.source = source;
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EffectiveConfig {
    pub cwd: PathBuf,
    pub cwd_source: ConfigSource,
    pub project_config: Option<PathBuf>,
    pub user_config: Option<PathBuf>,
    pub harness: Sourced<String>,
    pub transport: Sourced<String>,
    pub model: Sourced<Option<String>>,
    pub timeout: Sourced<String>,
    pub output: Sourced<OutputMode>,
    pub color: Sourced<bool>,
    pub verbose: Sourced<bool>,
    pub auth_profile: Sourced<Option<String>>,
    pub native_log: Sourced<Option<String>>,
    pub output_contract: Sourced<String>,
    pub permission_mode: Sourced<Option<String>>,
    pub native_max_turns: Sourced<Option<String>>,
    pub native_timeout: Sourced<Option<String>>,
    pub native_tool_timeout: Sourced<Option<String>>,
    pub native_output_bytes: Sourced<Option<String>>,
    pub native_profile: Sourced<String>,
    pub native_add_dirs: Sourced<Vec<String>>,
    pub native_allow_tools: Sourced<Vec<String>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserHarnessSelection {
    pub path: PathBuf,
    pub changed: bool,
}

/// Atomically selects the default user-scoped harness, model, and auth profile
/// as one bundle while preserving every unrelated valid user configuration
/// key. Omitted model and profile values remove stale saved selections.
///
/// # Errors
///
/// Returns `CONFIG_INVALID` for an unsupported harness, a symlink destination,
/// invalid existing configuration, or any bounded filesystem failure.
pub fn write_user_harness(
    config: &EffectiveConfig,
    harness: &str,
    model: Option<&str>,
    auth_profile: Option<&str>,
) -> Result<UserHarnessSelection, RunnerError> {
    if !matches!(harness, "openprose" | "agents-sdk" | "prime" | "omp" | "codex" | "claude") {
        return Err(RunnerError::config(format!(
            "unsupported default harness {harness:?}; expected openprose, prime, omp, codex, or claude"
        )));
    }
    let path = config
        .user_config
        .clone()
        .ok_or_else(|| RunnerError::config("user configuration path is unavailable"))?;
    let parent = path
        .parent()
        .ok_or_else(|| RunnerError::config("user configuration has no parent directory"))?;
    prepare_private_config_parent(parent)?;
    refuse_symlinked_config_destination(&path)?;
    let existing_bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(error) => {
            return Err(RunnerError::config(format!(
                "cannot read user configuration {}: {error}",
                path.display()
            )));
        }
    };
    let existing = decode_configuration(&existing_bytes, &path)?.to_owned();
    let mut table = if existing.trim().is_empty() {
        toml::map::Map::new()
    } else {
        let lines = validate_flat_toml(&existing, &path)?;
        let values: FileConfig = toml::from_str(&existing).map_err(|_| {
            config_line_error(
                &path,
                1,
                "Configuration does not match the supported flat TOML subset.",
            )
        })?;
        let mut validated = config.clone();
        apply_file(
            &mut validated,
            LoadedFileConfig { values, lines },
            ConfigSource::file(ConfigSourceKind::UserFile, &path),
        )?;
        toml::from_str::<toml::Table>(&existing).map_err(|_| {
            config_line_error(
                &path,
                1,
                "Configuration does not match the supported flat TOML subset.",
            )
        })?
    };
    let already_selected = table.get("harness").and_then(toml::Value::as_str) == Some(harness)
        && table.get("model").and_then(toml::Value::as_str) == model
        && table.get("auth_profile").and_then(toml::Value::as_str) == auth_profile;
    if already_selected {
        return Ok(UserHarnessSelection {
            path,
            changed: false,
        });
    }
    table.insert(
        "harness".to_owned(),
        toml::Value::String(harness.to_owned()),
    );
    match model {
        Some(value) => {
            table.insert("model".to_owned(), toml::Value::String(value.to_owned()));
        }
        None => {
            table.remove("model");
        }
    }
    match auth_profile {
        Some(value) => {
            table.insert(
                "auth_profile".to_owned(),
                toml::Value::String(value.to_owned()),
            );
        }
        None => {
            table.remove("auth_profile");
        }
    }
    let bytes = toml::to_string(&table)
        .map_err(|error| {
            RunnerError::config(format!("cannot serialize user configuration: {error}"))
        })?
        .into_bytes();
    atomic_user_config_write(&path, &bytes)?;
    Ok(UserHarnessSelection { path, changed: true })
}

fn atomic_user_config_write(path: &Path, bytes: &[u8]) -> Result<(), RunnerError> {
    let parent = path.parent().ok_or_else(|| RunnerError::config("user configuration has no parent directory"))?;
    let temporary = parent.join(format!(".cli.toml.{}.tmp", uuid::Uuid::now_v7()));
    let write_result = (|| -> std::io::Result<()> {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        let mut file = options.open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        // Reauthenticate both names at the last practical boundary before the
        // atomic replacement. This preserves the same fail-closed behavior as
        // the Bun implementation if a local actor substitutes a direct parent
        // or destination symlink while the new bytes are being prepared.
        harden_config_parent_io(parent)?;
        refuse_symlinked_config_destination_io(path)?;
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temporary);
        return Err(RunnerError::config(format!(
            "cannot atomically update user configuration {}: {error}",
            path.display()
        )));
    }
    Ok(())
}

fn prepare_private_config_parent(parent: &Path) -> Result<(), RunnerError> {
    create_private_config_parent_io(parent).map_err(|error| {
        RunnerError::config(format!(
            "cannot create user configuration directory {}: {error}",
            parent.display()
        ))
    })?;
    harden_config_parent_io(parent).map_err(|error| {
        RunnerError::config(format!(
            "cannot secure user configuration directory {}: {error}",
            parent.display()
        ))
    })
}

fn create_private_config_parent_io(parent: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt as _;

        let mut builder = fs::DirBuilder::new();
        builder.recursive(true).mode(0o700);
        builder.create(parent)
    }
    #[cfg(not(unix))]
    {
        fs::create_dir_all(parent)
    }
}

fn refuse_symlinked_config_destination(path: &Path) -> Result<(), RunnerError> {
    refuse_symlinked_config_destination_io(path).map_err(|error| {
        RunnerError::config(format!(
            "cannot authenticate user configuration {}: {error}",
            path.display()
        ))
    })
}

fn refuse_symlinked_config_destination_io(path: &Path) -> std::io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "refusing to replace a symlinked destination",
        )),
        Ok(metadata) if !metadata.is_file() => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "destination exists but is not a regular file",
        )),
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn refuse_symlinked_config_parent_io(parent: &Path) -> std::io::Result<()> {
    let metadata = fs::symlink_metadata(parent)?;
    if metadata.file_type().is_symlink() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "refusing to use a symlinked direct parent",
        ));
    }
    if !metadata.is_dir() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "direct parent is not a directory",
        ));
    }
    Ok(())
}

fn harden_config_parent_io(parent: &Path) -> std::io::Result<()> {
    refuse_symlinked_config_parent_io(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;

        let mut permissions = fs::symlink_metadata(parent)?.permissions();
        permissions.set_mode(0o700);
        fs::set_permissions(parent, permissions)?;
    }
    refuse_symlinked_config_parent_io(parent)
}

impl EffectiveConfig {
    fn defaults(
        cwd: PathBuf,
        project_config: Option<PathBuf>,
        user_config: Option<PathBuf>,
    ) -> Self {
        Self {
            cwd,
            cwd_source: ConfigSource::new(
                ConfigSourceKind::Default,
                Some("process cwd".to_owned()),
            ),
            project_config,
            user_config,
            harness: Sourced {
                value: "openprose".to_owned(),
                source: ConfigSource::default(),
            },
            transport: Sourced {
                value: "auto".to_owned(),
                source: ConfigSource::default(),
            },
            model: Sourced {
                value: None,
                source: ConfigSource::default(),
            },
            timeout: Sourced {
                value: "10m".to_owned(),
                source: ConfigSource::default(),
            },
            output: Sourced {
                value: OutputMode::Human,
                source: ConfigSource::default(),
            },
            color: Sourced {
                value: false,
                source: ConfigSource::default(),
            },
            verbose: Sourced {
                value: false,
                source: ConfigSource::default(),
            },
            native_log:Sourced {value:None,source:ConfigSource::default()},
            output_contract: Sourced { value:if crate::kernel_startup::PUBLISHED_KERNEL_STARTUP && !cfg!(test) { "native" } else { "image-envelope" }.into(),source:ConfigSource::default() },
            native_max_turns: Sourced {value:None,source:ConfigSource::default()},
            native_timeout: Sourced {value:None,source:ConfigSource::default()},
            native_tool_timeout: Sourced {value:None,source:ConfigSource::default()},
            native_output_bytes: Sourced {value:None,source:ConfigSource::default()},
            native_profile: Sourced {value:"default".into(),source:ConfigSource::default()},
            native_add_dirs: Sourced {value:vec![],source:ConfigSource::default()},
            native_allow_tools: Sourced {value:vec![],source:ConfigSource::default()},
            permission_mode: Sourced { value:None, source:ConfigSource::default() },
            auth_profile: Sourced {
                value: None,
                source: ConfigSource::default(),
            },
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    service_environment: Option<String>,
    harness: Option<String>,
    transport: Option<String>,
    model: Option<String>,
    timeout: Option<String>,
    output: Option<String>,
    color: Option<bool>,
    verbose: Option<bool>,
    auth_profile: Option<String>,
    native_log: Option<String>,
    output_contract: Option<String>,
    permission_mode: Option<String>,
    native_max_turns: Option<String>,
    native_timeout: Option<String>,
    native_tool_timeout: Option<String>,
    native_output_bytes: Option<String>,
    native_profile: Option<String>,
    native_add_dirs: Option<Vec<String>>,
    native_allow_tools: Option<Vec<String>>,
}

struct LoadedFileConfig {
    values: FileConfig,
    lines: BTreeMap<String, usize>,
}

const FILE_CONFIG_KEYS: &[&str] = &[
    "service_environment",
    "harness",
    "transport",
    "model",
    "timeout",
    "output",
    "color",
    "verbose",
    "auth_profile",
    "native_log",
    "output_contract",
    "permission_mode",
    "native_max_turns",
    "native_timeout",
    "native_tool_timeout",
    "native_output_bytes",
    "native_profile",
    "native_add_dirs",
    "native_allow_tools",
];

fn config_line_error(path: &Path, line: usize, reason: impl Into<String>) -> RunnerError {
    RunnerError::config(reason).with_detail("source", format!("{}:{line}", path.display()))
}

fn decode_configuration<'a>(bytes: &'a [u8], path: &Path) -> Result<&'a str, RunnerError> {
    std::str::from_utf8(bytes).map_err(|error| {
        let line = std::str::from_utf8(&bytes[..error.valid_up_to()])
            .expect("UTF-8 validation guarantees the valid prefix")
            .chars()
            .filter(|character| *character == '\n')
            .count()
            + 1;
        config_line_error(path, line, "Configuration file is not valid UTF-8.")
    })
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FileValueKind {
    String,
    Boolean,
}

fn horizontal_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t')
}

fn trailing_content_is_allowed(value: &[u8], mut offset: usize) -> bool {
    while value
        .get(offset)
        .is_some_and(|byte| horizontal_space(*byte))
    {
        offset += 1;
    }
    offset == value.len() || value.get(offset) == Some(&b'#')
}

fn scan_basic_string(value: &[u8]) -> Result<usize, &'static str> {
    let mut index = 1;
    while index < value.len() {
        match value[index] {
            b'"' => return Ok(index + 1),
            0..=0x1f | 0x7f => {
                return Err("Configuration strings cannot contain control characters.");
            }
            b'\\' => {
                let Some(escape) = value.get(index + 1).copied() else {
                    return Err("Basic string contains an unsupported escape.");
                };
                if matches!(escape, b'"' | b'\\' | b'b' | b't' | b'n' | b'f' | b'r') {
                    index += 2;
                    continue;
                }
                if matches!(escape, b'u' | b'U') {
                    let width = if escape == b'u' { 4 } else { 8 };
                    let start = index + 2;
                    let Some(digits) = value.get(start..start + width) else {
                        return Err("Basic string contains an invalid Unicode escape.");
                    };
                    if !digits.iter().all(u8::is_ascii_hexdigit) {
                        return Err("Basic string contains an invalid Unicode escape.");
                    }
                    let code_point = digits.iter().fold(0_u32, |value, digit| {
                        value * 16 + (*digit as char).to_digit(16).unwrap()
                    });
                    if char::from_u32(code_point).is_none() {
                        return Err("Basic string contains an invalid Unicode escape.");
                    }
                    index = start + width;
                    continue;
                }
                return Err("Basic string contains an unsupported escape.");
            }
            _ => index += 1,
        }
    }
    Err("Basic string must close on the same physical line.")
}

fn scan_literal_string(value: &[u8]) -> Result<usize, &'static str> {
    let mut index = 1;
    while index < value.len() {
        match value[index] {
            b'\'' => return Ok(index + 1),
            0..=0x1f | 0x7f => {
                return Err("Configuration strings cannot contain control characters.");
            }
            _ => index += 1,
        }
    }
    Err("Literal string must close on the same physical line.")
}

fn validate_flat_toml(source: &str, path: &Path) -> Result<BTreeMap<String, usize>, RunnerError> {
    let mut lines = BTreeMap::new();
    for (offset, raw_physical_line) in source.split('\n').enumerate() {
        let line_number = offset + 1;
        let raw = raw_physical_line
            .strip_suffix('\r')
            .unwrap_or(raw_physical_line);
        if raw.as_bytes().contains(&b'\r') {
            return Err(config_line_error(
                path,
                line_number,
                "Configuration line contains an unsupported carriage return.",
            ));
        }
        let bytes = raw.as_bytes();
        let mut cursor = 0;
        while bytes
            .get(cursor)
            .is_some_and(|byte| horizontal_space(*byte))
        {
            cursor += 1;
        }
        if cursor == bytes.len() || bytes.get(cursor) == Some(&b'#') {
            continue;
        }
        let key_start = cursor;
        let valid_start = bytes
            .get(cursor)
            .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_');
        if !valid_start {
            return Err(config_line_error(
                path,
                line_number,
                "Configuration line must contain one known bare-key assignment.",
            ));
        }
        cursor += 1;
        while bytes
            .get(cursor)
            .is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
        {
            cursor += 1;
        }
        let key = &raw[key_start..cursor];
        while bytes
            .get(cursor)
            .is_some_and(|byte| horizontal_space(*byte))
        {
            cursor += 1;
        }
        if bytes.get(cursor) != Some(&b'=') {
            return Err(config_line_error(
                path,
                line_number,
                "Configuration line must contain one known bare-key assignment.",
            ));
        }
        cursor += 1;
        while bytes
            .get(cursor)
            .is_some_and(|byte| horizontal_space(*byte))
        {
            cursor += 1;
        }
        if !FILE_CONFIG_KEYS.contains(&key) {
            return Err(config_line_error(
                path,
                line_number,
                "Configuration contains an unknown key.",
            ));
        }
        if lines.insert(key.to_owned(), line_number).is_some() {
            return Err(
                RunnerError::config(format!("Duplicate configuration key: {key}."))
                    .with_detail("source", format!("{}:{line_number}", path.display())),
            );
        }
        let value = &bytes[cursor..];
        if matches!(key, "native_add_dirs" | "native_allow_tools") {
            let parsed: toml::Table = raw.parse().map_err(|_|config_line_error(path,line_number,"Expected a single-line string array."))?;
            let array = parsed.get(key).and_then(toml::Value::as_array).ok_or_else(||config_line_error(path,line_number,"Expected a single-line string array."))?;
            if !array.iter().all(|v|v.as_str().is_some()) {return Err(config_line_error(path,line_number,"Expected a string array."));}
            continue;
        }
        let (end, literal, value_kind) = match value.first() {
            Some(b'"') => (
                scan_basic_string(value)
                    .map_err(|reason| config_line_error(path, line_number, reason))?,
                false,
                FileValueKind::String,
            ),
            Some(b'\'') => (
                scan_literal_string(value)
                    .map_err(|reason| config_line_error(path, line_number, reason))?,
                true,
                FileValueKind::String,
            ),
            _ if value.starts_with(b"true") => (4, false, FileValueKind::Boolean),
            _ if value.starts_with(b"false") => (5, false, FileValueKind::Boolean),
            _ => {
                return Err(config_line_error(
                    path,
                    line_number,
                    "Configuration value must be true, false, or a single-line string.",
                ));
            }
        };
        if !trailing_content_is_allowed(value, end) {
            let reason = if literal {
                "Literal strings cannot contain apostrophes."
            } else {
                "Unexpected content after configuration value."
            };
            return Err(config_line_error(path, line_number, reason));
        }
        let requires_boolean = matches!(key, "color" | "verbose");
        if requires_boolean && value_kind != FileValueKind::Boolean {
            return Err(config_line_error(
                path,
                line_number,
                format!("Configuration key {key} requires a boolean value."),
            ));
        }
        if !requires_boolean && value_kind != FileValueKind::String {
            return Err(config_line_error(
                path,
                line_number,
                format!("Configuration key {key} requires a string value."),
            ));
        }
    }
    Ok(lines)
}

/// Resolves the closed configuration stack and canonical working directory.
///
/// # Errors
///
/// Returns `CONFIG_INVALID` for an invalid working directory, configuration
/// file, or recognized environment/flag value.
pub fn resolve_config(
    flags: &GlobalFlags,
    system: &SystemContext,
) -> Result<EffectiveConfig, RunnerError> {
    let requested_cwd = flags.cwd.as_ref().map_or_else(
        || system.current_dir.clone(),
        |cwd| {
            if cwd.is_absolute() {
                cwd.clone()
            } else {
                system.current_dir.join(cwd)
            }
        },
    );
    let cwd = fs::canonicalize(&requested_cwd).map_err(|error| {
        RunnerError::config(format!(
            "working directory {} does not exist or cannot be resolved: {error}",
            requested_cwd.display()
        ))
    })?;
    if !cwd.is_dir() {
        return Err(RunnerError::config(format!(
            "working directory {} is not a directory",
            cwd.display()
        )));
    }

    let project_config = discover_project_config(&cwd)?;
    let user_config_candidate = system.user_config_path()?;
    let user_config = user_config_candidate
        .is_file()
        .then(|| fs::canonicalize(&user_config_candidate).unwrap_or(user_config_candidate.clone()));

    let mut config =
        EffectiveConfig::defaults(cwd, project_config.clone(), Some(user_config_candidate));
    if flags.cwd.is_some() {
        config.cwd_source = ConfigSource::flag("--cwd");
    }

    // Same physical file is loaded once. If it appears in both roles, the
    // nearest-project role is authoritative.
    if user_config.as_ref() != project_config.as_ref() {
        if let Some(path) = &user_config {
            let file = load_file(path)?;
            apply_file(
                &mut config,
                file,
                ConfigSource::file(ConfigSourceKind::UserFile, path),
            )?;
        }
    }
    if let Some(path) = &project_config {
        let file = load_file(path)?;
        apply_file(
            &mut config,
            file,
            ConfigSource::file(ConfigSourceKind::ProjectFile, path),
        )?;
    }
    apply_environment(&mut config, &system.environment)?;
    apply_flags(&mut config, flags)?;
    if config.native_output_bytes.value.is_some() && config.output_contract.value != "native" { return Err(RunnerError::config("Native output bytes require native output mode.")); }
    if config.harness.value != "agents-sdk" && (config.native_max_turns.value.is_some() || config.native_timeout.value.is_some() || config.native_tool_timeout.value.is_some()) {return Err(RunnerError::config("Native budgets require agents-sdk."));}
    if config.native_profile.value != "default" && config.harness.value != "claude" {return Err(RunnerError::config("Native workspace profile requires Claude."));}
    if config.native_profile.value == "default" && (!config.native_add_dirs.value.is_empty() || !config.native_allow_tools.value.is_empty()) {return Err(RunnerError::config("Native directory/tool options require claude-workspace-tools."));}
    for directory in &mut config.native_add_dirs.value {
        let path=Path::new(directory);let path=if path.is_absolute(){path.to_owned()}else{config.cwd.join(path)};
        let resolved=fs::canonicalize(&path).map_err(|_|RunnerError::config("Native additional directory does not exist."))?;
        if !resolved.is_dir(){return Err(RunnerError::config("Native additional path is not a directory."));}
        *directory=resolved.to_string_lossy().into_owned();
    }
    Ok(config)
}

fn discover_project_config(cwd: &Path) -> Result<Option<PathBuf>, RunnerError> {
    let mut cursor = Some(cwd);
    while let Some(directory) = cursor {
        let candidate = directory.join(".prose").join("cli.toml");
        if candidate.is_file() {
            let canonical = fs::canonicalize(&candidate).map_err(|error| {
                RunnerError::config(format!(
                    "cannot resolve project configuration {}: {error}",
                    candidate.display()
                ))
            })?;
            return Ok(Some(canonical));
        }
        if directory.join(".git").exists() {
            break;
        }
        cursor = directory.parent();
    }
    Ok(None)
}

fn load_file(path: &Path) -> Result<LoadedFileConfig, RunnerError> {
    let bytes = fs::read(path).map_err(|error| {
        RunnerError::config(format!(
            "cannot read configuration {}: {error}",
            path.display()
        ))
    })?;
    let source = decode_configuration(&bytes, path)?;
    let lines = validate_flat_toml(source, path)?;
    let values = toml::from_str(source).map_err(|_| {
        config_line_error(
            path,
            1,
            "Configuration does not match the supported flat TOML subset.",
        )
    })?;
    Ok(LoadedFileConfig { values, lines })
}

fn file_value_error(
    source: &ConfigSource,
    lines: &BTreeMap<String, usize>,
    key: &str,
    reason: impl Into<String>,
) -> RunnerError {
    let error = RunnerError::config(reason);
    match (source.location.as_ref(), lines.get(key)) {
        (Some(location), Some(line)) => error.with_detail("source", format!("{location}:{line}")),
        _ => error,
    }
}

fn apply_file(
    target: &mut EffectiveConfig,
    loaded: LoadedFileConfig,
    source: ConfigSource,
) -> Result<(), RunnerError> {
    let LoadedFileConfig {
        values: source_values,
        lines,
    } = loaded;
    if let Some(value) = source_values.service_environment {
        if source.kind != ConfigSourceKind::UserFile || !matches!(value.as_str(), "production" | "staging") {
            return Err(file_value_error(&source, &lines, "service_environment", "Service environment must be production or staging and may only be set in user configuration."));
        }
    }
    if let Some(value) = source_values.harness {
        target.harness.replace(
            validate_harness("harness", value).map_err(|_| {
                file_value_error(
                    &source,
                    &lines,
                    "harness",
                    "Configuration key harness contains an unsupported value.",
                )
            })?,
            source.clone(),
        );
    }
    if let Some(value) = source_values.transport {
        target.transport.replace(
            nonempty("transport", value).map_err(|_| {
                file_value_error(
                    &source,
                    &lines,
                    "transport",
                    "Configuration key transport must not be empty.",
                )
            })?,
            source.clone(),
        );
    }
    if let Some(value) = source_values.model {
        target.model.replace(
            Some(nonempty("model", value).map_err(|_| {
                file_value_error(
                    &source,
                    &lines,
                    "model",
                    "Configuration key model must not be empty.",
                )
            })?),
            source.clone(),
        );
    }
    if let Some(value) = source_values.timeout {
        validate_timeout(&value).map_err(|_| {
            file_value_error(
                &source,
                &lines,
                "timeout",
                "Configuration key timeout contains an invalid duration.",
            )
        })?;
        target.timeout.replace(value, source.clone());
    }
    if let Some(value) = source_values.output {
        target.output.replace(
            OutputMode::parse(&value).map_err(|_| {
                file_value_error(
                    &source,
                    &lines,
                    "output",
                    "Configuration key output contains an unsupported value.",
                )
            })?,
            source.clone(),
        );
    }
    if let Some(value) = source_values.color {
        target.color.replace(value, source.clone());
    }
    if let Some(value) = source_values.verbose {
        target.verbose.replace(value, source.clone());
    }
    if let Some(value)=source_values.native_max_turns { validate_native_turns(&value)?; target.native_max_turns.replace(Some(value),source.clone()); }
    if let Some(value)=source_values.native_timeout { validate_native_timeout(&value)?; target.native_timeout.replace(Some(value),source.clone()); }
    if let Some(value)=source_values.native_tool_timeout { validate_native_timeout(&value)?; target.native_tool_timeout.replace(Some(value),source.clone()); }
    if let Some(value)=source_values.native_output_bytes { validate_native_output_bytes(&value)?; target.native_output_bytes.replace(Some(value),source.clone()); }
    if let Some(value)=source_values.native_profile {target.native_profile.replace(validate_native_profile(value)?,source.clone());}
    if let Some(value)=source_values.native_add_dirs {target.native_add_dirs.replace(validate_native_values(value)?,source.clone());}
    if let Some(value)=source_values.native_allow_tools {target.native_allow_tools.replace(validate_native_rules(value)?,source.clone());}
    if let Some(value)=source_values.native_log {target.native_log.replace(Some(value),source.clone());}
    if let Some(value) = source_values.output_contract { target.output_contract.replace(validate_output_contract(value)?,source.clone()); }
    if let Some(value) = source_values.permission_mode {
        target.permission_mode.replace(Some(validate_permission_mode(value)?), source.clone());
    }
    if let Some(value) = source_values.auth_profile {
        target.auth_profile.replace(
            Some(nonempty("auth_profile", value).map_err(|_| {
                file_value_error(
                    &source,
                    &lines,
                    "auth_profile",
                    "Configuration key auth_profile must not be empty.",
                )
            })?),
            source,
        );
    }
    Ok(())
}

fn apply_environment(
    target: &mut EffectiveConfig,
    environment: &BTreeMap<String, String>,
) -> Result<(), RunnerError> {
    if let Some(value) = environment.get("PROSE_HARNESS") {
        target.harness.replace(
            validate_harness("PROSE_HARNESS", value.clone())?,
            ConfigSource::environment("PROSE_HARNESS"),
        );
    }
    if let Some(value) = environment.get("PROSE_TRANSPORT") {
        target.transport.replace(
            nonempty("PROSE_TRANSPORT", value.clone())?,
            ConfigSource::environment("PROSE_TRANSPORT"),
        );
    }
    if let Some(value) = environment.get("PROSE_MODEL") {
        target.model.replace(
            Some(nonempty("PROSE_MODEL", value.clone())?),
            ConfigSource::environment("PROSE_MODEL"),
        );
    }
    if let Some(value) = environment.get("PROSE_TIMEOUT") {
        validate_timeout(value)?;
        target
            .timeout
            .replace(value.clone(), ConfigSource::environment("PROSE_TIMEOUT"));
    }
    if let Some(value) = environment.get("PROSE_OUTPUT") {
        target.output.replace(
            OutputMode::parse(value)?,
            ConfigSource::environment("PROSE_OUTPUT"),
        );
    }
    if let Some(value) = environment.get("PROSE_COLOR") {
        target.color.replace(
            parse_bool("PROSE_COLOR", value)?,
            ConfigSource::environment("PROSE_COLOR"),
        );
    }
    if let Some(value) = environment.get("PROSE_VERBOSE") {
        target.verbose.replace(
            parse_bool("PROSE_VERBOSE", value)?,
            ConfigSource::environment("PROSE_VERBOSE"),
        );
    }
    if let Some(value)=environment.get("PROSE_NATIVE_MAX_TURNS") { validate_native_turns(value)?; target.native_max_turns.replace(Some(value.clone()),ConfigSource::environment("PROSE_NATIVE_MAX_TURNS")); }
    if let Some(value)=environment.get("PROSE_NATIVE_TIMEOUT") { validate_native_timeout(value)?; target.native_timeout.replace(Some(value.clone()),ConfigSource::environment("PROSE_NATIVE_TIMEOUT")); }
    if let Some(value)=environment.get("PROSE_NATIVE_TOOL_TIMEOUT") { validate_native_timeout(value)?; target.native_tool_timeout.replace(Some(value.clone()),ConfigSource::environment("PROSE_NATIVE_TOOL_TIMEOUT")); }
    if let Some(value)=environment.get("PROSE_NATIVE_OUTPUT_BYTES") { validate_native_output_bytes(value)?; target.native_output_bytes.replace(Some(value.clone()),ConfigSource::environment("PROSE_NATIVE_OUTPUT_BYTES")); }
    if let Some(value)=environment.get("PROSE_NATIVE_PROFILE") {target.native_profile.replace(validate_native_profile(value.clone())?,ConfigSource::environment("PROSE_NATIVE_PROFILE"));}
    if let Some(value)=environment.get("PROSE_NATIVE_LOG"){target.native_log.replace(Some(value.clone()),ConfigSource::environment("PROSE_NATIVE_LOG"));}
    if let Some(value) = environment.get("PROSE_OUTPUT_CONTRACT") { target.output_contract.replace(validate_output_contract(value.clone())?,ConfigSource::environment("PROSE_OUTPUT_CONTRACT")); }
    if let Some(value) = environment.get("PROSE_PERMISSION_MODE") {
        target.permission_mode.replace(Some(validate_permission_mode(value.clone())?),ConfigSource::environment("PROSE_PERMISSION_MODE"));
    }
    if let Some(value) = environment.get("PROSE_AUTH_PROFILE") {
        target.auth_profile.replace(
            Some(nonempty("PROSE_AUTH_PROFILE", value.clone())?),
            ConfigSource::environment("PROSE_AUTH_PROFILE"),
        );
    }
    Ok(())
}

fn validate_native_profile(value:String)->Result<String,RunnerError>{
    if matches!(value.as_str(),"default"|"claude-workspace-tools"){Ok(value)}else{Err(RunnerError::config("Unknown native profile."))}
}
fn validate_native_values(values:Vec<String>)->Result<Vec<String>,RunnerError>{
    if values.iter().any(|v|v.trim().is_empty() || v.contains('\0')) {Err(RunnerError::config("Native directory/tool values must be nonempty and contain no NUL."))}else{Ok(values)}
}

fn validate_native_rules(values:Vec<String>)->Result<Vec<String>,RunnerError>{
    let values=validate_native_values(values)?;
    if values.iter().any(|v|v.starts_with('-')) {Err(RunnerError::config("Native tool rules must not start with '-'."))}else{Ok(values)}
}

fn validate_output_contract(value:String)->Result<String,RunnerError>{
 if matches!(value.as_str(),"native"|"image-envelope") {Ok(value)} else {Err(RunnerError::catalog(crate::error::ErrorCode::ConfigInvalid).with_detail("reason","Output contract must be native or image-envelope"))}
}

fn validate_permission_mode(value:String)->Result<String,RunnerError>{
    if matches!(value.as_str(),"default"|"acceptEdits"|"workspace-write"|"read-only") {Ok(value)} else {Err(RunnerError::catalog(crate::error::ErrorCode::ConfigInvalid).with_detail("reason","Permission mode must be default, acceptEdits, workspace-write, or read-only"))}
}

fn apply_flags(target: &mut EffectiveConfig, flags: &GlobalFlags) -> Result<(), RunnerError> {
    if let Some(value)=&flags.native_max_turns { validate_native_turns(value)?; target.native_max_turns.replace(Some(value.clone()),ConfigSource::flag("--native-max-turns")); }
    if let Some(value)=&flags.native_timeout { validate_native_timeout(value)?; target.native_timeout.replace(Some(value.clone()),ConfigSource::flag("--native-timeout")); }
    if let Some(value)=&flags.native_tool_timeout { validate_native_timeout(value)?; target.native_tool_timeout.replace(Some(value.clone()),ConfigSource::flag("--native-tool-timeout")); }
    if let Some(value)=&flags.native_output_bytes { validate_native_output_bytes(value)?; target.native_output_bytes.replace(Some(value.clone()),ConfigSource::flag("--native-output-bytes")); }
    if let Some(value)=&flags.native_profile {target.native_profile.replace(validate_native_profile(value.clone())?,ConfigSource::flag("--native-profile"));}
    if !flags.native_add_dirs.is_empty() {target.native_add_dirs.replace(validate_native_values(flags.native_add_dirs.clone())?,ConfigSource::flag("--native-add-dir"));}
    if !flags.native_allow_tools.is_empty() {target.native_allow_tools.replace(validate_native_rules(flags.native_allow_tools.clone())?,ConfigSource::flag("--native-allow-tool"));}
    if let Some(value)=&flags.native_log {target.native_log.replace(Some(value.clone()),ConfigSource::flag("--native-log"));}
    if let Some(value)=&flags.output_contract {target.output_contract.replace(validate_output_contract(value.clone())?,ConfigSource::flag("--output-contract"));}
    if let Some(value)=&flags.permission_mode {target.permission_mode.replace(Some(validate_permission_mode(value.clone())?),ConfigSource::flag("--permission-mode"));}
    if let Some(value) = &flags.harness {
        target.harness.replace(
            validate_harness("--harness", value.clone())?,
            ConfigSource::flag("--harness"),
        );
    }
    if let Some(value) = &flags.transport {
        target.transport.replace(
            nonempty("--transport", value.clone())?,
            ConfigSource::flag("--transport"),
        );
    }
    if let Some(value) = &flags.model {
        target.model.replace(
            Some(nonempty("--model", value.clone())?),
            ConfigSource::flag("--model"),
        );
    }
    if let Some(value) = &flags.auth_profile {
        target.auth_profile.replace(
            Some(nonempty("--auth-profile", value.clone())?),
            ConfigSource::flag("--auth-profile"),
        );
    }
    if let Some(value) = &flags.timeout {
        validate_timeout(value)?;
        target
            .timeout
            .replace(value.clone(), ConfigSource::flag("--timeout"));
    }
    if let Some(value) = flags.output {
        target.output.replace(value, ConfigSource::flag("--output"));
    }
    if flags.no_color {
        target
            .color
            .replace(false, ConfigSource::flag("--no-color"));
    }
    if flags.verbose {
        target
            .verbose
            .replace(true, ConfigSource::flag("--verbose"));
    }
    Ok(())
}

fn nonempty(name: &str, value: String) -> Result<String, RunnerError> {
    if value.is_empty() {
        Err(RunnerError::config(format!("{name} cannot be empty")))
    } else {
        Ok(value)
    }
}

fn validate_harness(name: &str, value: String) -> Result<String, RunnerError> {
    if matches!(
        value.as_str(),
        "openprose" | "agents-sdk" | "prime" | "omp" | "codex" | "claude" | "mock"
    ) {
        Ok(value)
    } else {
        Err(RunnerError::config(format!(
            "Unsupported harness {value:?}; expected openprose, prime, omp, codex, claude, or mock."
        ))
        .with_detail("source", name))
    }
}

fn parse_bool(name: &str, value: &str) -> Result<bool, RunnerError> {
    match value {
        "1" | "true" | "yes" | "on" | "always" => Ok(true),
        "0" | "false" | "no" | "off" | "never" => Ok(false),
        _ => Err(RunnerError::config(format!(
            "invalid {name} value {value:?}; expected true or false"
        ))),
    }
}

fn validate_timeout(value: &str) -> Result<(), RunnerError> {
    let suffix = ["ms", "s", "m", "h"]
        .into_iter()
        .find(|suffix| value.ends_with(suffix))
        .ok_or_else(|| {
            RunnerError::config(format!(
                "invalid timeout {value:?}; expected a positive duration such as 500ms, 30s, 10m, or 1h"
            ))
        })?;
    let number = &value[..value.len() - suffix.len()];
    let parsed = number.parse::<u64>().map_err(|_| {
        RunnerError::config(format!(
            "invalid timeout {value:?}; expected a positive duration such as 500ms, 30s, 10m, or 1h"
        ))
    })?;
    if parsed == 0 {
        return Err(RunnerError::config("timeout must be greater than zero"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::fs;
    use tempfile::TempDir;

    fn context(root: &Path, home: &Path) -> SystemContext {
        SystemContext {
            current_dir: root.to_owned(),
            home_dir: Some(home.to_owned()),
            xdg_config_home: Some(home.join("xdg")),
            appdata: None,
            environment: BTreeMap::new(),
            platform: Platform::Unix,
        }
    }

    #[test]
    fn native_output_budget_shared_fixture_and_precedence() {
        let f:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/native-output-budget.json")).unwrap();
        for v in f["valid"].as_array().unwrap(){assert_eq!(validate_native_output_bytes(v.as_str().unwrap()).unwrap().to_string(),v.as_str().unwrap());}
        for v in f["invalid"].as_array().unwrap(){assert!(validate_native_output_bytes(v.as_str().unwrap()).is_err(),"{v}");}
        let temp=TempDir::new().unwrap();let home=temp.path().join("home");
        fs::create_dir_all(temp.path().join(".prose")).unwrap();
        fs::write(temp.path().join(".prose/cli.toml"),"output_contract='native'\nnative_output_bytes='1048576'\n").unwrap();
        let mut sys=context(temp.path(),&home);let mut flags=GlobalFlags::default();
        assert_eq!(native_output_bytes(&resolve_config(&flags,&sys).unwrap()),1048576);
        sys.environment.insert("PROSE_NATIVE_OUTPUT_BYTES".into(),"134217728".into());
        assert_eq!(native_output_bytes(&resolve_config(&flags,&sys).unwrap()),134217728);
        flags.native_output_bytes=Some("268435456".into());
        let c=resolve_config(&flags,&sys).unwrap();assert_eq!(native_output_bytes(&c),268435456);assert_eq!(c.native_output_bytes.source,ConfigSource::flag("--native-output-bytes"));
        flags.output_contract=Some("image-envelope".into());assert!(resolve_config(&flags,&sys).is_err());
        let mut c=c;c.native_output_bytes.value=None;assert_eq!(native_output_bytes(&c),67108864);assert_eq!(native_output_limits(&c).unwrap()["captureEnabled"],false);
    }

    #[test]
    fn sdk_native_budget_fixture_and_precedence() {
        let fixture:Value=serde_json::from_str(include_str!("../../../../shared/fixtures/adapters/sdk-native-limits.json")).unwrap();
        for v in fixture["invalidTurns"].as_array().unwrap() {assert!(validate_native_turns(v.as_str().unwrap()).is_err());}
        for v in fixture["invalidTimeouts"].as_array().unwrap() {assert!(validate_native_timeout(v.as_str().unwrap()).is_err());}
        let temp=TempDir::new().unwrap();let home=temp.path().join("home");
        fs::create_dir_all(temp.path().join(".prose")).unwrap();
        fs::write(temp.path().join(".prose/cli.toml"),"harness='agents-sdk'\nnative_max_turns='30'\nnative_timeout='1s'\n").unwrap();
        let mut sys=context(temp.path(),&home);let mut flags=GlobalFlags::default();
        assert_eq!(native_limits(&resolve_config(&flags,&sys).unwrap()).unwrap()["maxTurns"],30);
        sys.environment.insert("PROSE_NATIVE_MAX_TURNS".into(),"35".into());
        sys.environment.insert("PROSE_NATIVE_TIMEOUT".into(),"1ms".into());
        assert_eq!(native_limits(&resolve_config(&flags,&sys).unwrap()).unwrap()["timeoutSeconds"],0.001);
        flags.native_max_turns=Some("40".into());flags.native_timeout=Some("5m".into());
        let cfg=resolve_config(&flags,&sys).unwrap();assert_eq!(native_limits(&cfg).unwrap(),fixture["override"]["limits"]);
        flags.harness=Some("claude".into());assert!(resolve_config(&flags,&sys).is_err());
        let mut cfg=cfg;cfg.native_max_turns.value=None;cfg.native_timeout.value=None;assert_eq!(native_limits(&cfg).unwrap(),fixture["defaults"]);
    }

    #[test]
    fn native_profile_precedence_arrays_and_directory_validation(){
        let temp=TempDir::new().unwrap();let home=temp.path().join("home");fs::create_dir_all(temp.path().join(".prose")).unwrap();fs::create_dir(temp.path().join("a b")).unwrap();
        fs::write(temp.path().join(".prose/cli.toml"),"harness='claude'\nnative_profile='claude-workspace-tools'\nnative_add_dirs=['a b']\nnative_allow_tools=['Read','Bash(git status:*)']\n").unwrap();
        let mut system=context(temp.path(),&home);let mut flags=GlobalFlags::default();
        let cfg=resolve_config(&flags,&system).unwrap();assert_eq!(cfg.native_add_dirs.value,vec![fs::canonicalize(temp.path().join("a b")).unwrap().to_string_lossy()]);assert_eq!(cfg.native_allow_tools.value.len(),2);
        system.environment.insert("PROSE_NATIVE_PROFILE".into(),"default".into());assert!(resolve_config(&flags,&system).is_err());
        flags.native_profile=Some("claude-workspace-tools".into());flags.native_allow_tools=vec!["Agent".into()];assert_eq!(resolve_config(&flags,&system).unwrap().native_allow_tools.value,vec!["Agent"]);
        flags.native_add_dirs=vec!["missing".into()];assert!(resolve_config(&flags,&system).is_err());
        flags.native_add_dirs=vec!["a b".into()];flags.harness=Some("codex".into());assert!(resolve_config(&flags,&system).is_err());
        assert!(validate_native_values(vec![" ".into()]).is_err());assert!(validate_native_rules(vec!["--dangerous".into()]).is_err());assert!(validate_native_profile("unknown".into()).is_err());
    }

    #[test]
    fn applies_closed_precedence_and_reports_every_source() {
        let temp = TempDir::new().unwrap();
        let project = temp.path().join("project");
        let child = project.join("deep").join("child");
        let home = temp.path().join("home");
        fs::create_dir_all(project.join(".git")).unwrap();
        fs::create_dir_all(project.join(".prose")).unwrap();
        fs::create_dir_all(&child).unwrap();
        fs::create_dir_all(home.join("xdg/openprose")).unwrap();
        fs::write(
            home.join("xdg/openprose/cli.toml"),
            "harness = \"claude\"\ntransport = \"user-transport\"\nmodel = \"user-model\"\n",
        )
        .unwrap();
        fs::write(
            project.join(".prose/cli.toml"),
            "harness = \"codex\"\ntransport = \"project-transport\"\n",
        )
        .unwrap();
        let mut system = context(&child, &home);
        system
            .environment
            .insert("PROSE_HARNESS".into(), "prime".into());
        system
            .environment
            .insert("PROSE_UNKNOWN".into(), "ignored".into());
        let flags = GlobalFlags {
            harness: Some("mock".into()),
            auth_profile: Some("flag-profile".into()),
            ..GlobalFlags::default()
        };

        let config = resolve_config(&flags, &system).unwrap();
        assert_eq!(config.cwd, fs::canonicalize(child).unwrap());
        assert_eq!(config.harness.value, "mock");
        assert_eq!(config.harness.source.kind, ConfigSourceKind::Flag);
        assert_eq!(config.transport.value, "project-transport");
        assert_eq!(config.transport.source.kind, ConfigSourceKind::ProjectFile);
        assert_eq!(config.model.value.as_deref(), Some("user-model"));
        assert_eq!(config.model.source.kind, ConfigSourceKind::UserFile);
        assert_eq!(config.timeout.source.kind, ConfigSourceKind::Default);
        assert_eq!(config.auth_profile.value.as_deref(), Some("flag-profile"));
        assert_eq!(config.auth_profile.source.kind, ConfigSourceKind::Flag);
        assert_eq!(
            config.auth_profile.source.location.as_deref(),
            Some("--auth-profile")
        );
    }

    #[test]
    fn cwd_is_applied_before_project_discovery_and_symlinks_are_resolved() {
        let temp = TempDir::new().unwrap();
        let project = temp.path().join("project");
        let elsewhere = temp.path().join("elsewhere");
        fs::create_dir_all(project.join(".git")).unwrap();
        fs::create_dir_all(project.join(".prose")).unwrap();
        fs::create_dir_all(project.join("nested")).unwrap();
        fs::create_dir_all(&elsewhere).unwrap();
        fs::write(project.join(".prose/cli.toml"), "harness = \"codex\"\n").unwrap();
        let flags = GlobalFlags {
            cwd: Some(project.join("nested")),
            ..GlobalFlags::default()
        };
        let config =
            resolve_config(&flags, &context(&elsewhere, &temp.path().join("home"))).unwrap();
        assert_eq!(config.harness.value, "codex");
        assert_eq!(config.harness.source.kind, ConfigSourceKind::ProjectFile);
    }

    #[test]
    fn vcs_boundary_stops_parent_configuration() {
        let temp = TempDir::new().unwrap();
        let parent = temp.path();
        let repository = parent.join("repo");
        let child = repository.join("child");
        fs::create_dir_all(parent.join(".prose")).unwrap();
        fs::write(parent.join(".prose/cli.toml"), "harness = \"wrong\"\n").unwrap();
        fs::create_dir_all(repository.join(".git")).unwrap();
        fs::create_dir_all(&child).unwrap();
        let config = resolve_config(
            &GlobalFlags::default(),
            &context(&child, &parent.join("home")),
        )
        .unwrap();
        assert_eq!(config.harness.value, "openprose");
    }

    #[test]
    fn user_configuration_roots_must_be_present_non_empty_and_absolute() {
        let temp = TempDir::new().unwrap();
        let absolute_home = temp.path().join("home");
        let cases = [
            SystemContext {
                current_dir: temp.path().to_owned(),
                home_dir: Some(absolute_home.clone()),
                xdg_config_home: Some(PathBuf::new()),
                appdata: None,
                environment: BTreeMap::new(),
                platform: Platform::Unix,
            },
            SystemContext {
                current_dir: temp.path().to_owned(),
                home_dir: Some(absolute_home),
                xdg_config_home: Some(PathBuf::from("relative")),
                appdata: None,
                environment: BTreeMap::new(),
                platform: Platform::Unix,
            },
            SystemContext {
                current_dir: temp.path().to_owned(),
                home_dir: None,
                xdg_config_home: None,
                appdata: None,
                environment: BTreeMap::new(),
                platform: Platform::Unix,
            },
            SystemContext {
                current_dir: temp.path().to_owned(),
                home_dir: Some(PathBuf::new()),
                xdg_config_home: None,
                appdata: None,
                environment: BTreeMap::new(),
                platform: Platform::MacOs,
            },
            SystemContext {
                current_dir: temp.path().to_owned(),
                home_dir: None,
                xdg_config_home: None,
                appdata: Some(PathBuf::from("relative")),
                environment: BTreeMap::new(),
                platform: Platform::Windows,
            },
        ];
        for system in cases {
            let error = resolve_config(&GlobalFlags::default(), &system).unwrap_err();
            assert_eq!(error.code, crate::ErrorCode::ConfigInvalid);
        }
    }

    #[test]
    fn unknown_file_keys_fail_closed_with_path() {
        let temp = TempDir::new().unwrap();
        fs::create_dir_all(temp.path().join(".prose")).unwrap();
        let path = temp.path().join(".prose/cli.toml");
        fs::write(&path, "mystery = true\n").unwrap();
        let error = resolve_config(
            &GlobalFlags::default(),
            &context(temp.path(), &temp.path().join("home")),
        )
        .unwrap_err();
        assert_eq!(error.code, crate::ErrorCode::ConfigInvalid);
        let details = error.details.unwrap();
        assert_eq!(details["reason"], "Configuration contains an unknown key.");
        assert_eq!(
            details["source"],
            format!("{}:1", fs::canonicalize(path).unwrap().display())
        );
    }

    #[test]
    fn implements_shared_portable_flat_toml_corpus_without_exposing_rejected_values() {
        let corpus: Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/config/flat-toml-v1.json"
        ))
        .unwrap();
        for accepted in corpus["accepted"].as_array().unwrap() {
            let temp = TempDir::new().unwrap();
            let home = temp.path().join("home");
            let config_path = home.join("xdg/openprose/cli.toml");
            fs::create_dir_all(config_path.parent().unwrap()).unwrap();
            fs::write(&config_path, accepted["source"].as_str().unwrap()).unwrap();
            let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home))
                .unwrap_or_else(|error| panic!("{}: {error:?}", accepted["id"]));
            for (key, expected) in accepted["values"].as_object().unwrap() {
                let actual = match key.as_str() {
                    "harness" => Value::String(config.harness.value.clone()),
                    "transport" => Value::String(config.transport.value.clone()),
                    "model" => config
                        .model
                        .value
                        .clone()
                        .map_or(Value::Null, Value::String),
                    "timeout" => Value::String(config.timeout.value.clone()),
                    "output" => serde_json::to_value(config.output.value).unwrap(),
                    "color" => Value::Bool(config.color.value),
                    "verbose" => Value::Bool(config.verbose.value),
                    "authProfile" => config
                        .auth_profile
                        .value
                        .clone()
                        .map_or(Value::Null, Value::String),
                    unknown => panic!("unknown shared expected key {unknown}"),
                };
                assert_eq!(actual, *expected, "{}: {key}", accepted["id"]);
            }
        }

        for rejected in corpus["rejected"].as_array().unwrap() {
            let temp = TempDir::new().unwrap();
            let home = temp.path().join("home");
            let config_path = home.join("xdg/openprose/cli.toml");
            fs::create_dir_all(config_path.parent().unwrap()).unwrap();
            fs::write(&config_path, rejected["source"].as_str().unwrap()).unwrap();
            let error =
                resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap_err();
            let details = error.details.unwrap();
            assert_eq!(
                error.code,
                crate::ErrorCode::ConfigInvalid,
                "{}",
                rejected["id"]
            );
            assert_eq!(details["reason"], rejected["reason"], "{}", rejected["id"]);
            assert_eq!(
                details["source"],
                format!(
                    "{}:{}",
                    fs::canonicalize(&config_path).unwrap().display(),
                    rejected["line"]
                ),
                "{}",
                rejected["id"]
            );
            if let Some(forbidden) = rejected.get("forbidden").and_then(Value::as_str) {
                assert!(!serde_json::to_string(&details).unwrap().contains(forbidden));
                fs::remove_file(&config_path).unwrap();
                let config =
                    resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();
                fs::write(&config_path, rejected["source"].as_str().unwrap()).unwrap();
                let write_error = write_user_harness(&config, "claude", None, None).unwrap_err();
                let write_details = write_error.details.unwrap();
                assert_eq!(write_details["reason"], rejected["reason"]);
                assert_eq!(
                    write_details["source"],
                    format!("{}:{}", config_path.display(), rejected["line"])
                );
                assert!(
                    !serde_json::to_string(&write_details)
                        .unwrap()
                        .contains(forbidden)
                );
                assert_eq!(
                    fs::read_to_string(&config_path).unwrap(),
                    rejected["source"].as_str().unwrap()
                );
            }
        }

        for rejected in corpus["binaryRejected"].as_array().unwrap() {
            let temp = TempDir::new().unwrap();
            let home = temp.path().join("home");
            let config_path = home.join("xdg/openprose/cli.toml");
            fs::create_dir_all(config_path.parent().unwrap()).unwrap();
            let source = rejected["sourceHex"]
                .as_str()
                .unwrap()
                .as_bytes()
                .chunks_exact(2)
                .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
                .collect::<Vec<_>>();
            fs::write(&config_path, &source).unwrap();
            let error =
                resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap_err();
            let details = error.details.unwrap();
            assert_eq!(details["reason"], rejected["reason"]);
            assert_eq!(
                details["source"],
                format!(
                    "{}:{}",
                    fs::canonicalize(&config_path).unwrap().display(),
                    rejected["line"]
                )
            );

            fs::remove_file(&config_path).unwrap();
            let config =
                resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();
            fs::write(&config_path, &source).unwrap();
            let write_error = write_user_harness(&config, "claude", None, None).unwrap_err();
            let write_details = write_error.details.unwrap();
            assert_eq!(write_details["reason"], rejected["reason"]);
            assert_eq!(
                write_details["source"],
                format!("{}:{}", config_path.display(), rejected["line"])
            );
            assert_eq!(fs::read(&config_path).unwrap(), source);
        }
    }

    #[test]
    fn unsupported_harness_values_fail_at_the_configuration_source() {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let config_path = home.join("xdg/openprose/cli.toml");
        fs::create_dir_all(config_path.parent().unwrap()).unwrap();
        fs::write(
            &config_path,
            "model = \"valid-model\"\n\nharness = \"nope\"\n",
        )
        .unwrap();

        let file_error =
            resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap_err();
        assert_eq!(file_error.code, crate::ErrorCode::ConfigInvalid);
        let file_details = file_error.details.unwrap();
        assert_eq!(
            file_details["reason"],
            "Configuration key harness contains an unsupported value."
        );
        assert_eq!(
            file_details["source"],
            format!("{}:3", fs::canonicalize(&config_path).unwrap().display())
        );

        fs::remove_file(&config_path).unwrap();
        let mut system = context(temp.path(), &home);
        system
            .environment
            .insert("PROSE_HARNESS".to_owned(), "nope".to_owned());
        let environment_error = resolve_config(&GlobalFlags::default(), &system).unwrap_err();
        assert_eq!(environment_error.code, crate::ErrorCode::ConfigInvalid);
        assert_eq!(
            environment_error.details.unwrap()["source"],
            "PROSE_HARNESS"
        );

        let flag_error = resolve_config(
            &GlobalFlags {
                harness: Some("nope".to_owned()),
                ..GlobalFlags::default()
            },
            &context(temp.path(), &home),
        )
        .unwrap_err();
        assert_eq!(flag_error.code, crate::ErrorCode::ConfigInvalid);
        assert_eq!(flag_error.details.unwrap()["source"], "--harness");
    }

    #[test]
    fn user_harness_selection_is_atomic_idempotent_and_preserves_valid_keys() {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let system = context(temp.path(), &home);
        let config = resolve_config(&GlobalFlags::default(), &system).unwrap();
        let path = home.join("xdg/openprose/cli.toml");

        let first = write_user_harness(&config, "claude", None, None).unwrap();
        assert!(first.changed);
        assert_eq!(first.path, path);
        assert_eq!(fs::read_to_string(&path).unwrap(), "harness = \"claude\"\n");

        fs::write(
            &path,
            "harness = \"claude\"\nmodel = \"fixture-model\"\ntimeout = \"30s\"\n",
        )
        .unwrap();
        assert!(
            write_user_harness(&config, "claude", None, None)
                .unwrap()
                .changed
        );
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            "harness = \"claude\"\ntimeout = \"30s\"\n"
        );

        assert!(
            write_user_harness(
                &config,
                "prime",
                Some("openai/gpt-5.4"),
                Some("prime-harness-login")
            )
            .unwrap()
            .changed
        );
        let selected: toml::Table = toml::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(selected["harness"].as_str(), Some("prime"));
        assert_eq!(selected["model"].as_str(), Some("openai/gpt-5.4"));
        assert_eq!(
            selected["auth_profile"].as_str(),
            Some("prime-harness-login")
        );
        assert_eq!(selected["timeout"].as_str(), Some("30s"));
        assert!(
            !write_user_harness(
                &config,
                "prime",
                Some("openai/gpt-5.4"),
                Some("prime-harness-login")
            )
            .unwrap()
            .changed
        );

        assert!(
            write_user_harness(&config, "codex", None, None)
                .unwrap()
                .changed
        );
        let updated: toml::Table = toml::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(updated["harness"].as_str(), Some("codex"));
        assert!(!updated.contains_key("model"));
        assert!(!updated.contains_key("auth_profile"));
        assert_eq!(updated["timeout"].as_str(), Some("30s"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn user_harness_selection_refuses_symlink_and_preserves_its_target() {
        use std::os::unix::fs::symlink;

        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();
        let path = home.join("xdg/openprose/cli.toml");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let target = temp.path().join("target.toml");
        fs::write(&target, "harness = \"openprose\"\n").unwrap();
        symlink(&target, &path).unwrap();

        let error = write_user_harness(&config, "claude", None, None).unwrap_err();
        assert_eq!(error.code, crate::ErrorCode::ConfigInvalid);
        assert_eq!(
            fs::read_to_string(&target).unwrap(),
            "harness = \"openprose\"\n"
        );
    }

    #[cfg(unix)]
    #[test]
    fn user_harness_selection_refuses_a_symlinked_direct_parent() {
        use std::os::unix::fs::PermissionsExt as _;
        use std::os::unix::fs::symlink;

        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let xdg = home.join("xdg");
        let redirected = temp.path().join("redirected-config");
        fs::create_dir_all(&xdg).unwrap();
        fs::create_dir_all(&redirected).unwrap();
        fs::set_permissions(&redirected, fs::Permissions::from_mode(0o755)).unwrap();
        symlink(&redirected, xdg.join("openprose")).unwrap();
        let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();

        let error = write_user_harness(&config, "claude", None, None).unwrap_err();
        assert_eq!(error.code, crate::ErrorCode::ConfigInvalid);
        assert!(!redirected.join("cli.toml").exists());
        assert_eq!(
            fs::metadata(&redirected).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }

    #[cfg(unix)]
    #[test]
    fn user_harness_selection_hardens_an_existing_config_directory() {
        use std::os::unix::fs::PermissionsExt as _;

        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let parent = home.join("xdg/openprose");
        fs::create_dir_all(&parent).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o777)).unwrap();
        let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();

        write_user_harness(&config, "claude", None, None).unwrap();

        assert_eq!(
            fs::metadata(&parent).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(parent.join("cli.toml"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );

        fs::set_permissions(&parent, fs::Permissions::from_mode(0o777)).unwrap();
        let unchanged = write_user_harness(&config, "claude", None, None).unwrap();
        assert!(!unchanged.changed);
        assert_eq!(
            fs::metadata(&parent).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::read_to_string(parent.join("cli.toml")).unwrap(),
            "harness = \"claude\"\n"
        );
    }

    #[test]
    fn user_harness_selection_refuses_a_non_directory_parent_without_mutation() {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let xdg = home.join("xdg");
        let parent = xdg.join("openprose");
        fs::create_dir_all(&xdg).unwrap();
        fs::write(&parent, "not a directory\n").unwrap();
        let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();

        let error = write_user_harness(&config, "claude", None, None).unwrap_err();

        assert_eq!(error.code, crate::ErrorCode::ConfigInvalid);
        assert_eq!(fs::read_to_string(&parent).unwrap(), "not a directory\n");
    }

    #[cfg(unix)]
    #[test]
    fn idempotent_user_harness_selection_refuses_a_symlinked_direct_parent() {
        use std::os::unix::fs::symlink;

        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let xdg = home.join("xdg");
        let redirected = temp.path().join("redirected-config");
        fs::create_dir_all(&xdg).unwrap();
        fs::create_dir_all(&redirected).unwrap();
        fs::write(redirected.join("cli.toml"), "harness = \"claude\"\n").unwrap();
        symlink(&redirected, xdg.join("openprose")).unwrap();
        let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();

        let error = write_user_harness(&config, "claude", None, None).unwrap_err();
        assert_eq!(error.code, crate::ErrorCode::ConfigInvalid);
        assert_eq!(
            fs::read_to_string(redirected.join("cli.toml")).unwrap(),
            "harness = \"claude\"\n"
        );
    }

    #[test]
    fn user_harness_selection_rejects_unknown_id_and_invalid_existing_config() {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let config = resolve_config(&GlobalFlags::default(), &context(temp.path(), &home)).unwrap();
        assert_eq!(
            write_user_harness(&config, "mystery", None, None)
                .unwrap_err()
                .code,
            crate::ErrorCode::ConfigInvalid
        );
        let path = home.join("xdg/openprose/cli.toml");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, "unknown = true\n").unwrap();
        assert_eq!(
            write_user_harness(&config, "claude", None, None)
                .unwrap_err()
                .code,
            crate::ErrorCode::ConfigInvalid
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), "unknown = true\n");
    }
}

pub(crate) fn validate_native_turns(value: &str) -> Result<u64, RunnerError> {
    if value.is_empty() || value.starts_with('0') || !value.bytes().all(|c| c.is_ascii_digit()) { return Err(RunnerError::config("Native max turns must be a positive safe integer.")); }
    value.parse::<u64>().ok().filter(|n| *n > 0 && *n <= 9_007_199_254_740_991).ok_or_else(|| RunnerError::config("Native max turns must be a positive safe integer."))
}
pub(crate) fn validate_native_timeout(value: &str) -> Result<u64, RunnerError> {
    let (n,m)=if let Some(n)=value.strip_suffix("ms") {(n,1)} else if let Some(n)=value.strip_suffix('s') {(n,1000)} else if let Some(n)=value.strip_suffix('m') {(n,60000)} else if let Some(n)=value.strip_suffix('h') {(n,3600000)} else {return Err(RunnerError::config("Native timeout requires ms, s, m or h."));};
    validate_native_turns(n)?.checked_mul(m).filter(|n| *n<=9_007_199_254_740_991).ok_or_else(||RunnerError::config("Native timeout exceeds safe milliseconds."))
}
pub(crate) fn native_limits(config: &EffectiveConfig)->Option<serde_json::Value> {
 if config.harness.value!="agents-sdk" {return None;}
 let ms=config.native_timeout.value.as_deref().map(|v|validate_native_timeout(v).expect("validated")).unwrap_or(180000);
 let tool_ms=config.native_tool_timeout.value.as_deref().map(|v|validate_native_timeout(v).expect("validated")).unwrap_or(30000);
 let tool_seconds=if tool_ms%1000==0 {serde_json::json!(tool_ms/1000)}else{serde_json::json!(tool_ms as f64/1000.0)};
 let seconds=if ms%1000==0 {serde_json::json!(ms/1000)}else{serde_json::json!(ms as f64/1000.0)};
 Some(serde_json::json!({"maxTurns":config.native_max_turns.value.as_deref().map(|v|validate_native_turns(v).expect("validated")).unwrap_or(20),"timeoutSeconds":seconds,"toolTimeoutSeconds":tool_seconds,"maxOutputTokens":12000}))
}

const DEFAULT_NATIVE_OUTPUT_BYTES: usize = 67_108_864;
pub(crate) fn validate_native_output_bytes(value: &str) -> Result<usize, RunnerError> {
 validate_native_turns(value).ok().filter(|n| (1_048_576..=268_435_456).contains(n)).map(|n| n as usize).ok_or_else(|| RunnerError::config("Native output bytes must be decimal bytes from 1048576 through 268435456."))
}
pub(crate) fn native_output_bytes(config: &EffectiveConfig) -> usize {
 config.native_output_bytes.value.as_deref().map(|v| validate_native_output_bytes(v).expect("validated output budget")).unwrap_or(DEFAULT_NATIVE_OUTPUT_BYTES)
}
pub(crate) fn native_output_limits(config: &EffectiveConfig) -> Option<serde_json::Value> {
 (config.output_contract.value == "native").then(|| serde_json::json!({"maxAggregateStdoutBytes":native_output_bytes(config),"maxNativeCaptureBytes":native_output_bytes(config),"captureEnabled":config.native_log.value.is_some()}))
}

/// User-only service selection, separate from harness configuration.
#[derive(Debug, Clone)]
pub struct ServiceSelection {
    pub environment: String,
    pub source: &'static str,
}

fn read_service_config(
    system: &SystemContext,
) -> Result<(PathBuf, String, FileConfig), RunnerError> {
    let path = system.user_config_path()?;
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(_) => return Err(RunnerError::config("Cannot read user configuration.")),
    };
    let text = decode_configuration(&bytes, &path)?.to_owned();
    let lines = validate_flat_toml(&text, &path)?;
    let values: FileConfig = toml::from_str(&text).map_err(|_| {
        config_line_error(
            &path,
            1,
            "Configuration does not match the supported flat TOML subset.",
        )
    })?;
    let mut validated =
        EffectiveConfig::defaults(system.current_dir.clone(), None, Some(path.clone()));
    apply_file(
        &mut validated,
        LoadedFileConfig {
            values: values.clone(),
            lines,
        },
        ConfigSource::file(ConfigSourceKind::UserFile, &path),
    )?;
    Ok((path, text, values))
}

/// Reads only the user's service selection; workspace files cannot redirect it.
///
/// # Errors
/// Returns `CONFIG_INVALID` if the user path or configuration is invalid.
pub fn resolve_service_selection(system: &SystemContext) -> Result<ServiceSelection, RunnerError> {
    let (_, _, values) = read_service_config(system)?;
    Ok(ServiceSelection {
        source: if values.service_environment.is_some() {
            "user-config"
        } else {
            "default"
        },
        environment: values
            .service_environment
            .unwrap_or_else(|| "production".into()),
    })
}

/// Saves or removes only the service selection, preserving unrelated text.
///
/// # Errors
/// Returns `CONFIG_INVALID` for invalid values, unsafe paths, invalid existing
/// configuration, or an atomic filesystem update failure.
pub fn write_service_selection(
    system: &SystemContext,
    environment: Option<&str>,
) -> Result<ServiceSelection, RunnerError> {
    if environment.is_some_and(|value| !matches!(value, "production" | "staging")) {
        return Err(RunnerError::config(
            "Service environment must be production or staging.",
        ));
    }
    let path = system.user_config_path()?;
    let parent = path
        .parent()
        .ok_or_else(|| RunnerError::config("user configuration has no parent directory"))?;
    prepare_private_config_parent(parent)?;
    refuse_symlinked_config_destination(&path)?;
    let (_, existing, values) = read_service_config(system)?;
    if values.service_environment.as_deref() != environment {
        let mut updated = String::new();
        for line in existing.split_inclusive('\n') {
            if line
                .split_once('=')
                .is_some_and(|(key, _)| key.trim() == "service_environment")
            {
                if let Some((_, comment)) = line.split_once('#') {
                    updated.push('#');
                    updated.push_str(comment);
                }
            } else {
                updated.push_str(line);
            }
        }
        if let Some(environment) = environment {
            if !updated.is_empty() && !updated.ends_with('\n') {
                updated.push('\n');
            }
            updated.push_str(&format!("service_environment = \"{environment}\"\n"));
        }
        atomic_user_config_write(&path, updated.as_bytes())?;
    }
    Ok(ServiceSelection {
        environment: environment.unwrap_or("production").into(),
        source: if environment.is_some() {
            "user-config"
        } else {
            "default"
        },
    })
}

#[cfg(test)]
mod service_selection_tests {
    use super::*;
    fn system(root: &Path) -> SystemContext {
        SystemContext {
            current_dir: root.to_owned(),
            home_dir: Some(root.join("home")),
            xdg_config_home: Some(root.join("xdg")),
            appdata: None,
            environment: BTreeMap::new(),
            platform: Platform::Unix,
        }
    }
    #[test]
    fn service_selection_is_user_only_and_bad_config_never_defaults() {
        let root = tempfile::tempdir().unwrap();
        let system = system(root.path());
        assert_eq!(
            resolve_service_selection(&system).unwrap().environment,
            "production"
        );
        fs::create_dir_all(root.path().join(".prose")).unwrap();
        fs::write(
            root.path().join(".prose/cli.toml"),
            "service_environment = \"staging\"\n",
        )
        .unwrap();
        assert_eq!(
            resolve_service_selection(&system).unwrap().environment,
            "production"
        );
        assert!(resolve_config(&GlobalFlags::default(), &system).is_err());
        let path = system.user_config_path().unwrap();
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, "service_environment = \"other\"\n").unwrap();
        assert!(resolve_service_selection(&system).is_err());
        assert!(write_service_selection(&system, Some("production")).is_err());
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            "service_environment = \"other\"\n"
        );
    }
    #[test]
    fn service_selection_preserves_comments_and_harness_writer_preserves_selection() {
        let root = tempfile::tempdir().unwrap();
        let system = system(root.path());
        let path = system.user_config_path().unwrap();
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            &path,
            "# user note\nharness = \"codex\"\nservice_environment = \"staging\" # service note\n",
        )
        .unwrap();
        write_service_selection(&system, None).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            "# user note\nharness = \"codex\"\n# service note\n"
        );
        write_service_selection(&system, Some("staging")).unwrap();
        let config = resolve_config(&GlobalFlags::default(), &system).unwrap();
        write_user_harness(&config, "claude", None, None).unwrap();
        assert_eq!(
            resolve_service_selection(&system).unwrap().environment,
            "staging"
        );
    }
    #[cfg(unix)]
    #[test]
    fn service_selection_refuses_destination_and_parent_symlinks() {
        use std::os::unix::fs::symlink;
        let root = tempfile::tempdir().unwrap();
        let system = system(root.path());
        let path = system.user_config_path().unwrap();
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let target = root.path().join("target");
        fs::write(&target, "harness = \"codex\"\n").unwrap();
        symlink(&target, &path).unwrap();
        assert!(write_service_selection(&system, Some("staging")).is_err());
        assert_eq!(
            fs::read_to_string(&target).unwrap(),
            "harness = \"codex\"\n"
        );
        fs::remove_file(&path).unwrap();
        fs::remove_dir(path.parent().unwrap()).unwrap();
        let redirected = root.path().join("redirected");
        fs::create_dir(&redirected).unwrap();
        symlink(&redirected, path.parent().unwrap()).unwrap();
        assert!(write_service_selection(&system, Some("staging")).is_err());
        assert!(!redirected.join("cli.toml").exists());
    }
}
