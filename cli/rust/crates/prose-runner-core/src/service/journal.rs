//! The local run journal.
//!
//! `$XDG_STATE_HOME/openprose/cli/<environment>/runs/<session>.json` (Linux and
//! any platform with `XDG_STATE_HOME`; otherwise `~/.local/state` on Linux,
//! `~/Library/Application Support` on macOS and `%LOCALAPPDATA%` on Windows).
//! `<environment>` is `production` (a `dev-endpoint` build's custom origin:
//! `custom-<origin digest>`). Directories are
//! 0700 and files 0600; entries hold `{session, runId, createdAt, lastSequence,
//! sourceSha256}` and never a credential. Entries older than 30 days are pruned.
use super::Environment;
use crate::{RunnerError, SystemContext};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

/// One journal entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub session: String,
    pub run_id: Option<String>,
    pub created_at: String,
    pub last_sequence: u64,
    pub source_sha256: Option<String>,
}

impl Entry {
    pub fn to_value(&self) -> Value {
        json!({
            "session": self.session,
            "runId": self.run_id,
            "createdAt": self.created_at,
            "lastSequence": self.last_sequence,
            "sourceSha256": self.source_sha256,
        })
    }

    pub fn from_value(value: &Value) -> Option<Self> {
        let session = value["session"]
            .as_str()
            .filter(|session| valid_session(session))?
            .to_owned();
        Some(Self {
            session,
            run_id: match &value["runId"] {
                Value::Null => None,
                Value::String(run) => Some(run.clone()),
                _ => return None,
            },
            created_at: value["createdAt"].as_str()?.to_owned(),
            last_sequence: value["lastSequence"]
                .as_u64()
                .filter(|sequence| *sequence <= super::http::MAX_SAFE_INTEGER)?,
            source_sha256: match &value["sourceSha256"] {
                Value::Null => None,
                Value::String(digest) => Some(digest.clone()),
                _ => return None,
            },
        })
    }
}

/// A lower-case UUID (the journal file name).
pub fn valid_session(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok_and(|parsed| parsed.hyphenated().to_string() == value)
}

fn failure(reason: &str) -> RunnerError {
    RunnerError::config(format!("run journal: {reason}"))
}

/// The journal directory for one environment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Journal {
    root: Option<PathBuf>,
    directory: Option<PathBuf>,
}

impl Journal {
    pub fn for_environment(system: &SystemContext, environment: &Environment) -> Self {
        let state = system
            .environment
            .get("XDG_STATE_HOME")
            .filter(|value| Path::new(value).is_absolute())
            .map(PathBuf::from)
            // Windows: LOCALAPPDATA first, whether or not a home directory
            // is known (as in the Bun port).
            .or_else(|| {
                cfg!(windows)
                    .then(|| system.environment.get("LOCALAPPDATA"))
                    .flatten()
                    .filter(|value| !value.is_empty())
                    .map(PathBuf::from)
            })
            .or_else(|| {
                let home = system.home_dir.clone()?;
                Some(if cfg!(target_os = "macos") {
                    home.join("Library").join("Application Support")
                } else if cfg!(windows) {
                    home.join("AppData").join("Local")
                } else {
                    home.join(".local").join("state")
                })
            });
        let root = state.map(|state| state.join("openprose"));
        let directory = root.as_ref().map(|root| {
            root.join("cli")
                .join(environment.journal_component())
                .join("runs")
        });
        Self { root, directory }
    }

    /// A journal rooted at an explicit directory (tests).
    pub fn at(directory: PathBuf) -> Self {
        Self {
            root: Some(directory.clone()),
            directory: Some(directory),
        }
    }

    pub fn directory(&self) -> Option<&Path> {
        self.directory.as_deref()
    }

    fn ensure(&self) -> Result<&Path, RunnerError> {
        let (Some(root), Some(directory)) = (&self.root, &self.directory) else {
            return Err(failure("no state directory (set HOME or XDG_STATE_HOME)"));
        };
        std::fs::create_dir_all(directory)
            .map_err(|_| failure("cannot create the journal directory"))?;
        let mut current = directory.as_path();
        loop {
            private_directory(current)?;
            if current == root.as_path() {
                break;
            }
            match current.parent() {
                Some(parent) => current = parent,
                None => break,
            }
        }
        Ok(directory)
    }

    fn path(&self, session: &str) -> Result<PathBuf, RunnerError> {
        if !valid_session(session) {
            return Err(failure("invalid session id"));
        }
        Ok(self
            .directory
            .as_ref()
            .ok_or_else(|| failure("no state directory (set HOME or XDG_STATE_HOME)"))?
            .join(format!("{session}.json")))
    }

    /// Writes (creates or replaces) an entry atomically with mode 0600.
    pub fn write(&self, entry: &Entry) -> Result<(), RunnerError> {
        let directory = self.ensure()?;
        let path = self.path(&entry.session)?;
        let temporary = directory.join(format!(".{}.tmp", entry.session));
        let mut text = entry.to_value().to_string();
        text.push('\n');
        let _ = std::fs::remove_file(&temporary);
        write_private(&temporary, text.as_bytes())?;
        std::fs::rename(&temporary, &path).map_err(|_| failure("cannot replace a journal entry"))
    }

    /// Writes a raw fixture entry (validated) — test seams only use this.
    pub fn write_value(&self, value: &Value) -> Result<(), RunnerError> {
        let entry = Entry::from_value(value).ok_or_else(|| failure("invalid preset entry"))?;
        self.write(&entry)
    }

    /// Reads one entry by session.
    pub fn get(&self, session: &str) -> Result<Option<Entry>, RunnerError> {
        let path = self.path(session)?;
        match std::fs::read(&path) {
            Ok(bytes) => Ok(serde_json::from_slice::<Value>(&bytes)
                .ok()
                .as_ref()
                .and_then(Entry::from_value)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(failure("cannot read a journal entry")),
        }
    }

    /// Every readable entry, newest `createdAt` first (ties by session).
    pub fn entries(&self) -> Vec<Entry> {
        let Some(directory) = &self.directory else {
            return Vec::new();
        };
        let Ok(listing) = std::fs::read_dir(directory) else {
            return Vec::new();
        };
        let mut entries = listing
            .filter_map(Result::ok)
            .filter(|item| item.file_name().to_string_lossy().ends_with(".json"))
            .filter_map(|item| std::fs::read(item.path()).ok())
            .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .filter_map(|value| Entry::from_value(&value))
            .collect::<Vec<_>>();
        entries.sort_by(|left, right| {
            right
                .created_at
                .cmp(&left.created_at)
                .then_with(|| left.session.cmp(&right.session))
        });
        entries
    }

    /// The newest entry for `run_id`.
    pub fn find_run(&self, run_id: &str) -> Option<Entry> {
        self.entries()
            .into_iter()
            .find(|entry| entry.run_id.as_deref() == Some(run_id))
    }

    /// Removes entries created more than `days` before `now` (RFC 3339).
    pub fn prune(&self, now: &str, days: i64) -> usize {
        let Ok(now) = chrono::DateTime::parse_from_rfc3339(now) else {
            return 0;
        };
        let cutoff = now - chrono::Duration::days(days);
        let mut removed = 0;
        for entry in self.entries() {
            let old = chrono::DateTime::parse_from_rfc3339(&entry.created_at)
                .is_ok_and(|created| created < cutoff);
            if old
                && self
                    .path(&entry.session)
                    .and_then(|path| std::fs::remove_file(path).map_err(|_| failure("remove")))
                    .is_ok()
            {
                removed += 1;
            }
        }
        removed
    }
}

#[cfg(unix)]
fn private_directory(path: &Path) -> Result<(), RunnerError> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
        .map_err(|_| failure("cannot make the journal directory private"))
}

#[cfg(not(unix))]
fn private_directory(_: &Path) -> Result<(), RunnerError> {
    Ok(())
}

#[cfg(unix)]
fn write_private(path: &Path, bytes: &[u8]) -> Result<(), RunnerError> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|_| failure("cannot create a journal entry"))?;
    file.write_all(bytes)
        .and_then(|()| file.sync_all())
        .map_err(|_| failure("cannot write a journal entry"))
}

#[cfg(not(unix))]
fn write_private(path: &Path, bytes: &[u8]) -> Result<(), RunnerError> {
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| failure("cannot create a journal entry"))?;
    file.write_all(bytes)
        .map_err(|_| failure("cannot write a journal entry"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn journal_times_match_the_shared_vectors() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/journal-times.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            let parsed = chrono::DateTime::parse_from_rfc3339(case["value"].as_str().unwrap())
                .ok()
                .map(|time| time.timestamp_millis());
            assert_eq!(serde_json::json!(parsed), case["millis"], "{}", case["id"]);
        }
    }

    fn entry(session: &str, run: Option<&str>, created: &str) -> Entry {
        Entry {
            session: session.into(),
            run_id: run.map(str::to_owned),
            created_at: created.into(),
            last_sequence: 0,
            source_sha256: None,
        }
    }

    #[test]
    fn entries_are_private_and_findable() {
        let directory = tempfile::tempdir().unwrap();
        let journal = Journal::at(directory.path().join("runs"));
        let first = entry(
            "00000000-0000-4000-8000-000000000001",
            Some("run_a"),
            "2026-09-01T00:00:00.000Z",
        );
        let second = entry(
            "00000000-0000-4000-8000-000000000002",
            Some("run_a"),
            "2026-09-02T00:00:00.000Z",
        );
        journal.write(&first).unwrap();
        journal.write(&second).unwrap();
        assert_eq!(journal.find_run("run_a").unwrap().session, second.session);
        assert_eq!(journal.get(&first.session).unwrap().unwrap(), first);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let path = journal
                .directory()
                .unwrap()
                .join(format!("{}.json", first.session));
            assert_eq!(
                std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
                0o600
            );
            assert_eq!(
                std::fs::metadata(journal.directory().unwrap())
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
        }
        assert_eq!(journal.prune("2026-10-01T12:00:00.000Z", 30), 1);
        assert!(journal.get(&first.session).unwrap().is_none());
        assert!(journal.write(&entry("not-a-uuid", None, "x")).is_err());
    }
}
