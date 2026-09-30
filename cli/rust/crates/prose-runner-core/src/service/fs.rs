//! Local file helpers for service operations: bounded source reads (a path or
//! `-` for standard input), never-overwrite output files, and the
//! fresh-directory writer used by downloads.
use crate::RunnerError;
use std::fs::File;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

fn absolute(cwd: &Path, value: &str) -> PathBuf {
    let path = Path::new(value);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        cwd.join(path)
    }
}

/// Reads at most `max` bytes from `value` (a path relative to `cwd`, or `-`
/// for standard input). `label` names the argument in errors.
pub fn read_source(cwd: &Path, value: &str, max: u64, label: &str) -> Result<Vec<u8>, RunnerError> {
    let mut bytes = Vec::new();
    let read = if value == "-" {
        std::io::stdin()
            .lock()
            .take(max + 1)
            .read_to_end(&mut bytes)
    } else {
        let path = absolute(cwd, value);
        if !path.is_file() {
            return Err(invalid(format!(
                "{label} {value_quoted} is not a readable file",
                value_quoted = crate::error::quote(value)
            )));
        }
        File::open(&path).and_then(|file| file.take(max + 1).read_to_end(&mut bytes))
    };
    read.map_err(|_| {
        invalid(format!(
            "cannot read {label} {value_quoted}",
            value_quoted = crate::error::quote(value)
        ))
    })?;
    if bytes.len() as u64 > max {
        return Err(invalid(format!(
            "{label} {value_quoted} is larger than {max} bytes",
            value_quoted = crate::error::quote(value)
        )));
    }
    Ok(bytes)
}

/// Like [`read_source`], and the bytes must be UTF-8 text.
pub fn read_text(cwd: &Path, value: &str, max: u64, label: &str) -> Result<String, RunnerError> {
    String::from_utf8(read_source(cwd, value, max, label)?).map_err(|_| {
        invalid(format!(
            "{label} {value_quoted} is not UTF-8 text",
            value_quoted = crate::error::quote(value)
        ))
    })
}

/// Checks, without touching the file system beyond metadata reads, that
/// `value` (relative to `cwd`) can be created as a new file: it must name a
/// file (non-empty, no trailing `/`, final segment not `.` or `..`), must not
/// exist, and its parent directory must. Commands call this before any
/// request so a bad `--output-file` never reaches the service; the path is
/// resolved by the OS, never lexically normalized, in both ports.
pub fn check_new_file(cwd: &Path, value: &str) -> Result<PathBuf, RunnerError> {
    let last = value.rsplit('/').next().unwrap_or_default();
    if matches!(last, "" | "." | "..") {
        return Err(invalid(format!(
            "output file {value_quoted} must name a file, not a directory",
            value_quoted = crate::error::quote(value)
        )));
    }
    let path = absolute(cwd, value);
    if std::fs::symlink_metadata(&path).is_ok() {
        return Err(invalid(format!(
            "output file {value_quoted} already exists; choose a new path",
            value_quoted = crate::error::quote(value)
        )));
    }
    if !path.parent().is_some_and(Path::is_dir) {
        return Err(invalid(format!(
            "the directory for output file {value_quoted} does not exist",
            value_quoted = crate::error::quote(value)
        )));
    }
    Ok(path)
}

/// Creates `value` (relative to `cwd`) for writing after [`check_new_file`].
pub fn create_new_file(cwd: &Path, value: &str) -> Result<File, RunnerError> {
    let path = check_new_file(cwd, value)?;
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|_| {
            invalid(format!(
                "cannot create output file {value_quoted}",
                value_quoted = crate::error::quote(value)
            ))
        })
}

/// Writes `bytes` to a new file (see [`create_new_file`]).
pub fn write_new_file(cwd: &Path, value: &str, bytes: &[u8]) -> Result<(), RunnerError> {
    use std::io::Write;
    let mut file = create_new_file(cwd, value)?;
    file.write_all(bytes)
        .and_then(|()| file.sync_all())
        .map_err(|_| {
            invalid(format!(
                "cannot write output file {value_quoted}",
                value_quoted = crate::error::quote(value)
            ))
        })
}

/// A service-supplied relative path: no absolute path, backslash, `.`/`..`
/// segment, empty segment or control character (and on Windows no colon);
/// at most 1024 bytes.
pub fn valid_relative_path(value: &str) -> bool {
    // Windows: a colon names a drive or an alternate data stream.
    let drive_or_stream = cfg!(windows) && value.contains(':');
    !value.is_empty()
        && !drive_or_stream
        && value.len() <= 1024
        && !value.starts_with('/')
        && !value.contains('\\')
        && !value
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
        && value
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
        && Path::new(value)
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

/// A directory created by this invocation; files are created inside it only.
#[derive(Debug)]
pub struct FreshDirectory {
    root: PathBuf,
}

impl FreshDirectory {
    /// Creates `value` (relative to `cwd`). It must not exist; its parent must.
    pub fn create(cwd: &Path, value: &str) -> Result<Self, RunnerError> {
        let root = absolute(cwd, value);
        if std::fs::symlink_metadata(&root).is_ok() {
            return Err(invalid(format!(
                "output directory {value_quoted} already exists; choose a new directory",
                value_quoted = crate::error::quote(value)
            )));
        }
        if !root.parent().is_some_and(Path::is_dir) {
            return Err(invalid(format!(
                "the parent of output directory {value_quoted} does not exist",
                value_quoted = crate::error::quote(value)
            )));
        }
        std::fs::create_dir(&root).map_err(|_| {
            invalid(format!(
                "cannot create output directory {value_quoted}",
                value_quoted = crate::error::quote(value)
            ))
        })?;
        Ok(Self { root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Creates `relative` inside the directory (never overwriting).
    pub fn create_file(&self, relative: &str) -> Result<File, RunnerError> {
        if !valid_relative_path(relative) {
            return Err(
                RunnerError::catalog(crate::ErrorCode::ServiceProtocolInvalid)
                    .with_detail("reason", "the service named an unsafe output path"),
            );
        }
        let path = self.root.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|_| invalid("cannot create an output subdirectory"))?;
            let canonical_parent = parent
                .canonicalize()
                .map_err(|_| invalid("cannot resolve an output subdirectory"))?;
            let canonical_root = self
                .root
                .canonicalize()
                .map_err(|_| invalid("cannot resolve the output directory"))?;
            if !canonical_parent.starts_with(&canonical_root) {
                return Err(invalid("an output path escapes the output directory"));
            }
        }
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|_| {
                invalid(format!(
                    "cannot create output file {relative_quoted}",
                    relative_quoted = crate::error::quote(relative)
                ))
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn fresh_directories_and_files_never_overwrite() {
        let temporary = tempfile::tempdir().unwrap();
        let fresh = FreshDirectory::create(temporary.path(), "out").unwrap();
        fresh
            .create_file("a/b.txt")
            .unwrap()
            .write_all(b"x")
            .unwrap();
        assert!(fresh.create_file("a/b.txt").is_err());
        for bad in [
            "/etc/passwd",
            "../x",
            "a/../../x",
            "a//b",
            "a\\b",
            "",
            "a/./b",
        ] {
            assert!(fresh.create_file(bad).is_err(), "{bad}");
        }
        assert!(FreshDirectory::create(temporary.path(), "out").is_err());
        assert!(FreshDirectory::create(temporary.path(), "missing/out").is_err());
        write_new_file(temporary.path(), "file.txt", b"1").unwrap();
        assert!(write_new_file(temporary.path(), "file.txt", b"2").is_err());
        assert_eq!(
            read_source(temporary.path(), "file.txt", 1, "FILE").unwrap(),
            b"1"
        );
        assert!(read_source(temporary.path(), "file.txt", 0, "FILE").is_err());
        assert!(read_source(temporary.path(), "absent", 10, "FILE").is_err());
    }
}
