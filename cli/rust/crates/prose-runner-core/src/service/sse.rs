//! Bounded server-sent events reader.
//!
//! Lines end at LF (a preceding CR is dropped). `event`, `data` and `id`
//! fields are kept, comment lines (`: heartbeat`) are ignored, a blank line
//! dispatches an event whose data buffer is non-empty, and an unterminated
//! event at end of stream is discarded. Each event's data is at most
//! `maxEventBytes` (1 MiB); larger events fail with
//! `SERVICE_RESPONSE_TOO_LARGE`. The reader never retries.
use crate::error::ErrorCode;
use crate::{CancellationToken, RunnerError};
use serde_json::Value;
use std::io::Read;
use std::sync::mpsc::{Receiver, RecvTimeoutError, channel};
use std::time::{Duration, Instant};

/// One dispatched event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SseEvent {
    pub id: Option<String>,
    /// The `event` field, or `message` when absent.
    pub event: String,
    pub data: String,
}

impl SseEvent {
    /// The data parsed as JSON, or `SERVICE_PROTOCOL_INVALID`.
    pub fn json(&self) -> Result<Value, RunnerError> {
        super::http::parse_json(self.data.as_bytes())
            .ok_or_else(|| RunnerError::catalog(ErrorCode::ServiceProtocolInvalid))
    }
}

/// How a stream ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamEnd {
    /// Clean end of stream.
    Closed,
    /// The transport failed after the response started.
    Disconnected,
    /// No bytes (not even a heartbeat) arrived within the idle timeout.
    Idle,
}

/// Next item from the reader.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SseItem {
    Event(SseEvent),
    End(StreamEnd),
}

/// A chunk from a byte source.
#[derive(Debug)]
pub enum Chunk {
    Data(Vec<u8>),
    End(StreamEnd),
}

/// A source of stream bytes.
pub trait ChunkSource: Send {
    fn next_chunk(&mut self, cancellation: &CancellationToken) -> Chunk;
}

/// Reads a network body on a helper thread so cancellation and the idle
/// timeout are observed promptly.
pub struct ThreadSource {
    receiver: Receiver<std::io::Result<Vec<u8>>>,
    idle: Duration,
}

impl std::fmt::Debug for ThreadSource {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ThreadSource")
            .field("idle", &self.idle)
            .finish_non_exhaustive()
    }
}

impl ThreadSource {
    pub fn spawn(mut reader: Box<dyn Read + Send + Sync>, idle: Duration) -> Self {
        let (sender, receiver) = channel();
        std::thread::spawn(move || {
            let mut buffer = vec![0_u8; 16 * 1024];
            loop {
                match reader.read(&mut buffer) {
                    Ok(0) => {
                        let _ = sender.send(Ok(Vec::new()));
                        return;
                    }
                    Ok(count) => {
                        if sender.send(Ok(buffer[..count].to_vec())).is_err() {
                            return;
                        }
                    }
                    Err(error) => {
                        let _ = sender.send(Err(error));
                        return;
                    }
                }
            }
        });
        Self { receiver, idle }
    }
}

impl ChunkSource for ThreadSource {
    fn next_chunk(&mut self, cancellation: &CancellationToken) -> Chunk {
        let deadline = Instant::now() + self.idle;
        loop {
            if cancellation.is_cancelled() {
                return Chunk::End(StreamEnd::Disconnected);
            }
            let now = Instant::now();
            if now >= deadline {
                return Chunk::End(StreamEnd::Idle);
            }
            match self
                .receiver
                .recv_timeout((deadline - now).min(Duration::from_millis(50)))
            {
                Ok(Ok(bytes)) if bytes.is_empty() => return Chunk::End(StreamEnd::Closed),
                Ok(Ok(bytes)) => return Chunk::Data(bytes),
                Ok(Err(error))
                    if error.kind() == std::io::ErrorKind::TimedOut
                        || error.kind() == std::io::ErrorKind::WouldBlock =>
                {
                    return Chunk::End(StreamEnd::Idle);
                }
                Ok(Err(_)) | Err(RecvTimeoutError::Disconnected) => {
                    return Chunk::End(StreamEnd::Disconnected);
                }
                Err(RecvTimeoutError::Timeout) => {}
            }
        }
    }
}

/// Replays fixture frames; runs `on_complete` once every frame was delivered.
pub struct FixtureSource {
    frames: std::vec::IntoIter<Vec<u8>>,
    end: StreamEnd,
    on_complete: Option<Box<dyn FnMut() + Send>>,
}

impl std::fmt::Debug for FixtureSource {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("FixtureSource")
            .field("end", &self.end)
            .finish_non_exhaustive()
    }
}

impl FixtureSource {
    pub fn new(frames: Vec<Vec<u8>>, end: StreamEnd, on_complete: Box<dyn FnMut() + Send>) -> Self {
        Self {
            frames: frames.into_iter(),
            end,
            on_complete: Some(on_complete),
        }
    }
}

impl ChunkSource for FixtureSource {
    fn next_chunk(&mut self, _: &CancellationToken) -> Chunk {
        if let Some(frame) = self.frames.next() {
            return Chunk::Data(frame);
        }
        if let Some(mut hook) = self.on_complete.take() {
            hook();
        }
        Chunk::End(self.end)
    }
}

/// Serializes fixture frames (`service-fixture.schema.json` `sse.frames`).
pub fn fixture_bytes(frames: &Value) -> Vec<Vec<u8>> {
    frames
        .as_array()
        .map(|frames| frames.iter().map(frame_bytes).collect())
        .unwrap_or_default()
}

fn frame_bytes(frame: &Value) -> Vec<u8> {
    if let Some(raw) = frame["raw"].as_str() {
        return raw.as_bytes().to_vec();
    }
    let mut text = String::new();
    if let Some(comment) = frame["comment"].as_str() {
        text.push_str(": ");
        text.push_str(comment);
        text.push('\n');
    }
    if let Some(id) = frame["id"].as_str() {
        text.push_str("id: ");
        text.push_str(id);
        text.push('\n');
    }
    if let Some(event) = frame["event"].as_str() {
        text.push_str("event: ");
        text.push_str(event);
        text.push('\n');
    }
    match frame.get("data") {
        Some(Value::String(data)) => {
            for line in data.split('\n') {
                text.push_str("data: ");
                text.push_str(line);
                text.push('\n');
            }
        }
        Some(value) => {
            text.push_str("data: ");
            text.push_str(&value.to_string());
            text.push('\n');
        }
        None => {}
    }
    text.push('\n');
    text.into_bytes()
}

/// A pull parser over a chunk source.
pub struct SseReader {
    source: Box<dyn ChunkSource>,
    cancellation: CancellationToken,
    max_event: usize,
    pending: Vec<u8>,
    id: Option<String>,
    event: Option<String>,
    data: Option<String>,
    ended: Option<StreamEnd>,
}

impl std::fmt::Debug for SseReader {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SseReader")
            .field("max_event", &self.max_event)
            .finish_non_exhaustive()
    }
}

impl SseReader {
    pub fn new(
        source: Box<dyn ChunkSource>,
        cancellation: CancellationToken,
        max_event: usize,
    ) -> Self {
        Self {
            source,
            cancellation,
            max_event,
            pending: Vec::new(),
            id: None,
            event: None,
            data: None,
            ended: None,
        }
    }

    /// The next event or the end of the stream. Cancellation is reported as
    /// `CANCELLED`, oversize events as `SERVICE_RESPONSE_TOO_LARGE`, and
    /// non-UTF-8 lines as `SERVICE_PROTOCOL_INVALID`.
    pub fn next_item(&mut self) -> Result<SseItem, RunnerError> {
        loop {
            if self.cancellation.is_cancelled() {
                return Err(RunnerError::catalog(ErrorCode::Cancelled));
            }
            if let Some(position) = self.pending.iter().position(|byte| *byte == b'\n') {
                let mut line = self.pending.drain(..=position).collect::<Vec<_>>();
                line.pop();
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                if let Some(event) = self.line(&line)? {
                    return Ok(SseItem::Event(event));
                }
                continue;
            }
            if self.pending.len() > self.max_event + 64 {
                return Err(RunnerError::catalog(ErrorCode::ServiceResponseTooLarge));
            }
            if let Some(end) = self.ended {
                return Ok(SseItem::End(end));
            }
            match self.source.next_chunk(&self.cancellation) {
                Chunk::Data(bytes) => self.pending.extend_from_slice(&bytes),
                Chunk::End(end) => {
                    if self.cancellation.is_cancelled() {
                        return Err(RunnerError::catalog(ErrorCode::Cancelled));
                    }
                    // An unterminated trailing line or event is discarded.
                    self.pending.clear();
                    self.ended = Some(end);
                }
            }
        }
    }

    fn line(&mut self, line: &[u8]) -> Result<Option<SseEvent>, RunnerError> {
        if line.is_empty() {
            let event = self.event.take();
            let id = self.id.take();
            return Ok(self.data.take().map(|data| SseEvent {
                id,
                event: event.unwrap_or_else(|| "message".into()),
                data,
            }));
        }
        if line[0] == b':' {
            return Ok(None);
        }
        let text = std::str::from_utf8(line)
            .map_err(|_| RunnerError::catalog(ErrorCode::ServiceProtocolInvalid))?;
        let (field, value) = match text.split_once(':') {
            Some((field, value)) => (field, value.strip_prefix(' ').unwrap_or(value)),
            None => (text, ""),
        };
        match field {
            "event" => self.event = Some(value.to_owned()),
            "id" if !value.contains('\0') => self.id = Some(value.to_owned()),
            "data" => {
                let size = self.data.as_ref().map_or(0, |data| data.len() + 1) + value.len();
                if size > self.max_event {
                    return Err(RunnerError::catalog(ErrorCode::ServiceResponseTooLarge));
                }
                match &mut self.data {
                    Some(data) => {
                        data.push('\n');
                        data.push_str(value);
                    }
                    None => self.data = Some(value.to_owned()),
                }
            }
            _ => {}
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn reader(frames: &Value, end: StreamEnd, max: usize) -> SseReader {
        SseReader::new(
            Box::new(FixtureSource::new(
                fixture_bytes(frames),
                end,
                Box::new(|| {}),
            )),
            CancellationToken::default(),
            max,
        )
    }

    #[test]
    fn parses_events_ignores_heartbeats_and_reports_the_end() {
        let mut reader = reader(
            &json!([{"comment":"heartbeat"},{"id":"1","event":"status","data":{"status":"running"}},
                    {"raw":"event: text_chunk\r\ndata: a\r\ndata: b\r\n\r\n"},{"raw":"data: partial"}]),
            StreamEnd::Disconnected,
            1024,
        );
        let SseItem::Event(first) = reader.next_item().unwrap() else {
            panic!()
        };
        assert_eq!(
            (first.id.as_deref(), first.event.as_str()),
            (Some("1"), "status")
        );
        assert_eq!(first.json().unwrap()["status"], "running");
        let SseItem::Event(second) = reader.next_item().unwrap() else {
            panic!()
        };
        assert_eq!(
            (second.event.as_str(), second.data.as_str()),
            ("text_chunk", "a\nb")
        );
        assert_eq!(
            reader.next_item().unwrap(),
            SseItem::End(StreamEnd::Disconnected)
        );
    }

    #[test]
    fn oversize_events_fail_closed() {
        let mut reader = reader(&json!([{"data":"x".repeat(100)}]), StreamEnd::Closed, 64);
        assert_eq!(
            reader.next_item().unwrap_err().code,
            ErrorCode::ServiceResponseTooLarge
        );
    }

    #[test]
    fn cancellation_is_observed_between_frames() {
        let token = CancellationToken::default();
        let hook_token = token.clone();
        let mut reader = SseReader::new(
            Box::new(FixtureSource::new(
                fixture_bytes(&json!([{"data":"1"}])),
                StreamEnd::Idle,
                Box::new(move || hook_token.cancel()),
            )),
            token,
            1024,
        );
        assert!(matches!(reader.next_item().unwrap(), SseItem::Event(_)));
        assert_eq!(reader.next_item().unwrap_err().code, ErrorCode::Cancelled);
    }
}
