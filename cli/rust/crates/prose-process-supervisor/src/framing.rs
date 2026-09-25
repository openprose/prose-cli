use std::io::Read;
use std::sync::mpsc::SyncSender;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamLimits {
    pub max_record_bytes: usize,
    pub max_stdout_bytes: usize,
    pub max_stderr_bytes: usize,
    pub max_queued_records: usize,
}

impl Default for StreamLimits {
    fn default() -> Self {
        Self {
            max_record_bytes: 1024 * 1024,
            max_stdout_bytes: 64 * 1024 * 1024,
            max_stderr_bytes: 8 * 1024 * 1024,
            max_queued_records: 256,
        }
    }
}

#[derive(Debug)]
pub(crate) enum ReaderMessage {
    Record(Vec<u8>),
    StdoutEof,
    StdoutTruncated {
        observed: usize,
    },
    StdoutLimit {
        record: bool,
        observed: usize,
        limit: usize,
    },
    StdoutIo,
    Stderr(Vec<u8>),
    StderrEof,
    StderrLimit,
    StderrIo,
}

pub(crate) fn read_jsonl(
    mut reader: impl Read,
    limits: StreamLimits,
    sender: &SyncSender<ReaderMessage>,
) {
    let mut chunk = [0_u8; 8192];
    let mut record = Vec::new();
    let mut aggregate = 0_usize;
    loop {
        let count = match reader.read(&mut chunk) {
            Ok(0) => {
                let message = if record.is_empty() {
                    ReaderMessage::StdoutEof
                } else {
                    ReaderMessage::StdoutTruncated {
                        observed: record.len(),
                    }
                };
                let _ = sender.send(message);
                return;
            }
            Ok(count) => count,
            Err(_) => {
                let _ = sender.send(ReaderMessage::StdoutIo);
                return;
            }
        };
        aggregate = match aggregate.checked_add(count) {
            Some(value) if value <= limits.max_stdout_bytes => value,
            _ => {
                let _ = sender.send(ReaderMessage::StdoutLimit {
                    record: false,
                    observed: aggregate.saturating_add(count),
                    limit: limits.max_stdout_bytes,
                });
                return;
            }
        };
        for byte in &chunk[..count] {
            if *byte == b'\n' {
                if record.last() == Some(&b'\r') {
                    record.pop();
                }
                if record.is_empty() || sender.send(ReaderMessage::Record(record)).is_err() {
                    return;
                }
                record = Vec::new();
            } else {
                if record.len() >= limits.max_record_bytes {
                    let _ = sender.send(ReaderMessage::StdoutLimit {
                        record: true,
                        observed: record.len().saturating_add(1),
                        limit: limits.max_record_bytes,
                    });
                    return;
                }
                record.push(*byte);
            }
        }
    }
}

pub(crate) fn read_stderr(
    mut reader: impl Read,
    limits: StreamLimits,
    sender: &SyncSender<ReaderMessage>,
) {
    let mut chunk = [0_u8; 8192];
    let mut aggregate = 0_usize;
    loop {
        let count = match reader.read(&mut chunk) {
            Ok(0) => {
                let _ = sender.send(ReaderMessage::StderrEof);
                return;
            }
            Ok(count) => count,
            Err(_) => {
                let _ = sender.send(ReaderMessage::StderrIo);
                return;
            }
        };
        aggregate = match aggregate.checked_add(count) {
            Some(value) if value <= limits.max_stderr_bytes => value,
            _ => {
                let _ = sender.send(ReaderMessage::StderrLimit);
                return;
            }
        };
        if sender
            .send(ReaderMessage::Stderr(chunk[..count].to_vec()))
            .is_err()
        {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::sync_channel;

    fn records(input: &[u8], limits: StreamLimits) -> Vec<ReaderMessage> {
        let (sender, receiver) = sync_channel(16);
        read_jsonl(input, limits, &sender);
        drop(sender);
        receiver.into_iter().collect()
    }

    #[test]
    fn fragmented_reader_accepts_crlf_without_changing_record_bytes() {
        let messages = records(b"{\"one\":1}\r\n{\"two\":2}\n", StreamLimits::default());
        assert!(matches!(&messages[0], ReaderMessage::Record(value) if value == br#"{"one":1}"#));
        assert!(matches!(&messages[1], ReaderMessage::Record(value) if value == br#"{"two":2}"#));
        assert!(matches!(messages[2], ReaderMessage::StdoutEof));
    }

    #[test]
    fn shared_output_diagnostic_limits() {
        let cases: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../shared/fixtures/transport-diagnostics.json"
        ))
        .unwrap();
        for case in cases
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["name"] != "json")
        {
            let result = records(
                case["input"].as_str().unwrap().as_bytes(),
                StreamLimits {
                    max_record_bytes: usize::try_from(case["recordLimit"].as_u64().unwrap())
                        .unwrap(),
                    max_stdout_bytes: usize::try_from(case["aggregateLimit"].as_u64().unwrap())
                        .unwrap(),
                    ..StreamLimits::default()
                },
            );
            match result.last().unwrap() {
                ReaderMessage::StdoutLimit {
                    record,
                    observed,
                    limit,
                } => {
                    assert_eq!(*record, case["name"] == "record");
                    assert!(*observed > *limit);
                    assert_eq!(
                        *limit,
                        usize::try_from(case["limitBytes"].as_u64().unwrap()).unwrap()
                    );
                }
                other => panic!("unexpected {other:?}"),
            }
        }
    }

    #[test]
    fn partial_eof_and_record_limit_are_distinct() {
        let truncated = records(b"{\"unfinished\"", StreamLimits::default());
        assert!(matches!(
            truncated[0],
            ReaderMessage::StdoutTruncated { .. }
        ));
        let limited = records(
            b"12345\n",
            StreamLimits {
                max_record_bytes: 4,
                ..StreamLimits::default()
            },
        );
        assert!(matches!(
            limited[0],
            ReaderMessage::StdoutLimit {
                record: true,
                observed: 5,
                limit: 4
            }
        ));
    }
}
