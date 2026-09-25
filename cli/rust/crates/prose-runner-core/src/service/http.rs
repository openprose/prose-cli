//! Service transport: bounded requests with client identity headers,
//! no redirects and no retries, the body-code-first error classifier, and the
//! test-seam fixture transport (`PROSE_TEST_SERVICE_FIXTURE`).
use super::{Environment, manifest, render, sse};
use crate::error::ErrorCode;
use crate::{CancellationToken, RunnerError, SystemContext};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::io::{Read, Write};
use std::time::{Duration, Instant};

/// Transport class limits (manifest `transportClasses`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransportClass {
    Account,
    Control,
    Listing,
    Stream,
    Download,
}

impl TransportClass {
    pub fn from_name(name: &str) -> Self {
        match name {
            "account" => Self::Account,
            "listing" => Self::Listing,
            "stream" => Self::Stream,
            "download" => Self::Download,
            _ => Self::Control,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Account => "account",
            Self::Control => "control",
            Self::Listing => "listing",
            Self::Stream => "stream",
            Self::Download => "download",
        }
    }

    fn limit(self, key: &str) -> u64 {
        manifest()["transportClasses"][self.name()][key]
            .as_u64()
            .unwrap_or(0)
    }

    /// Response cap for a buffered request of this class. Stream and download
    /// classes buffer only control-sized bodies (errors, manifests).
    pub fn max_response_bytes(self) -> u64 {
        match self {
            Self::Stream | Self::Download => Self::Control.limit("maxResponseBytes"),
            other => other.limit("maxResponseBytes"),
        }
    }

    pub fn timeout(self) -> Duration {
        Duration::from_millis(match self {
            Self::Stream | Self::Download => Self::Control.limit("timeoutMs"),
            other => other.limit("timeoutMs"),
        })
    }

    pub fn connect_timeout(self) -> Duration {
        Duration::from_millis(self.limit("connectTimeoutMs").max(1))
    }

    pub fn idle_timeout(self) -> Duration {
        Duration::from_millis(self.limit("idleTimeoutMs").max(1))
    }

    pub fn max_event_bytes() -> usize {
        usize::try_from(Self::Stream.limit("maxEventBytes")).unwrap_or(1 << 20)
    }
}

/// One service request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub method: String,
    /// The manifest path template (`/programs/{slug}`), used for classification.
    pub template: String,
    /// The concrete, percent-encoded path.
    pub path: String,
    pub query: Vec<(String, String)>,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
    pub bearer: bool,
    pub class: TransportClass,
}

impl Request {
    /// A request for manifest template `index` of `operation`.
    pub fn from_manifest(operation: &Value, index: usize, path: impl Into<String>) -> Self {
        let template = &operation["requests"][index];
        Self {
            method: template["method"].as_str().unwrap_or("GET").to_owned(),
            template: template["path"].as_str().unwrap_or_default().to_owned(),
            path: path.into(),
            query: Vec::new(),
            headers: Vec::new(),
            body: None,
            bearer: template["auth"] == "bearer",
            class: TransportClass::from_name(operation["transport"].as_str().unwrap_or("control")),
        }
    }

    #[must_use]
    pub fn query(mut self, name: &str, value: impl Into<String>) -> Self {
        self.query.push((name.to_owned(), value.into()));
        self
    }

    #[must_use]
    pub fn header(mut self, name: &str, value: impl Into<String>) -> Self {
        self.headers.push((name.to_owned(), value.into()));
        self
    }

    /// Sets a JSON body (compact serialization) and `Content-Type`.
    #[must_use]
    pub fn json_body(mut self, value: &Value) -> Self {
        self.body = Some(serde_json::to_vec(value).expect("JSON values serialize"));
        self
    }

    #[must_use]
    pub fn class(mut self, class: TransportClass) -> Self {
        self.class = class;
        self
    }

    fn has_header(&self, name: &str) -> bool {
        self.headers
            .iter()
            .any(|(key, _)| key.eq_ignore_ascii_case(name))
    }

    /// The exact headers sent, in order (credentials included).
    fn wire_headers(&self, token: Option<&str>) -> Vec<(String, String)> {
        let mut headers = client_headers();
        if !self.has_header("Accept") {
            headers.push(("Accept".into(), "application/json".into()));
        }
        if let Some(token) = token {
            headers.push(("Authorization".into(), format!("Bearer {token}")));
        }
        if self.body.is_some() && !self.has_header("Content-Type") {
            headers.push(("Content-Type".into(), "application/json".into()));
        }
        headers.extend(self.headers.iter().cloned());
        headers
    }

    fn url(&self, origin: &str) -> String {
        let mut url = format!("{origin}{}", self.path);
        for (index, (name, value)) in self.query.iter().enumerate() {
            url.push(if index == 0 { '?' } else { '&' });
            url.push_str(&encode_query(name));
            url.push('=');
            url.push_str(&encode_query(value));
        }
        url
    }
}

/// `X-OpenProse-Client: cli/<version>+rust` and `User-Agent: prose-cli/<version>`
///. Also sent by every account request.
pub fn client_headers() -> Vec<(String, String)> {
    let version = crate::runner::RUNNER_VERSION;
    vec![
        ("X-OpenProse-Client".into(), format!("cli/{version}+rust")),
        ("User-Agent".into(), format!("prose-cli/{version}")),
    ]
}

/// A buffered response.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    /// Lower-case header names.
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .get(&name.to_ascii_lowercase())
            .map(String::as_str)
    }

    /// The body as a JSON object, or `SERVICE_PROTOCOL_INVALID`.
    pub fn json_object(&self) -> Result<Map<String, Value>, RunnerError> {
        match parse_json(&self.body) {
            Some(Value::Object(object)) => Ok(object),
            _ => Err(RunnerError::catalog(ErrorCode::ServiceProtocolInvalid)
                .with_detail("reason", RESPONSE_NOT_OBJECT)),
        }
    }
}

/// Result of opening a stream.
#[derive(Debug)]
pub enum StreamOpen {
    /// A 2xx response whose body is read as server-sent events.
    Events {
        status: u16,
        headers: BTreeMap<String, String>,
        reader: sse::SseReader,
    },
    /// A non-2xx response, buffered under the control limit.
    Response(Response),
    /// The connection failed before any response headers arrived.
    Dropped,
}

/// A completed download.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Downloaded {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub bytes: u64,
    pub sha256: String,
}

fn problem(code: ErrorCode) -> RunnerError {
    RunnerError::catalog(code)
}

/// The `details.reason` of a transport failure: every service
/// failure carries `.problem.details`, including a connection that failed
/// before a complete response, where the service said nothing.
pub(crate) const TRANSPORT_REASON: &str =
    "the connection to the service failed before a complete response arrived";

/// `SERVICE_UNAVAILABLE` for a transport failure, with its reason.
fn unavailable() -> RunnerError {
    problem(ErrorCode::ServiceUnavailable).with_detail("reason", TRANSPORT_REASON)
}

/// The proxy for `url` from the environment, with the Bun build's `fetch`
/// semantics (`shared/fixtures/transport/proxy-selection.json`):
///
/// - `https_proxy`, then `HTTPS_PROXY`, for an `https` URL; `http_proxy`,
///   then `HTTP_PROXY`, for `http`; the first non-empty one wins, and `""` or
///   `''` means none. `ALL_PROXY` and SOCKS are never used.
/// - `no_proxy`, then `NO_PROXY` (the first non-empty one): a comma list of
///   entries, each trimmed; `*` bypasses every host; one leading `.` is
///   ignored; an entry matches its host and every subdomain, ignoring case.
pub(crate) fn proxy_for(url: &str, lookup: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    let (scheme, rest) = url.split_once("://")?;
    let authority = rest.split(['/', '?', '#']).next()?;
    let authority = authority.rsplit('@').next()?;
    let host = if let Some(bracketed) = authority.strip_prefix('[') {
        bracketed.split(']').next()?
    } else {
        authority.split(':').next()?
    }
    .to_ascii_lowercase();
    let first = |names: [&str; 2]| {
        names
            .into_iter()
            .find_map(|name| lookup(name).filter(|value| !value.is_empty()))
    };
    let proxy = match scheme.to_ascii_lowercase().as_str() {
        "https" => first(["https_proxy", "HTTPS_PROXY"]),
        "http" => first(["http_proxy", "HTTP_PROXY"]),
        _ => None,
    }?;
    if proxy == "\"\"" || proxy == "''" {
        return None;
    }
    if let Some(list) = first(["no_proxy", "NO_PROXY"]) {
        for entry in list.split(',') {
            let entry = entry.trim_matches(|c: char| c.is_ascii_whitespace());
            if entry == "*" {
                return None;
            }
            let entry = entry
                .strip_prefix('.')
                .unwrap_or(entry)
                .to_ascii_lowercase();
            if !entry.is_empty() && (host == entry || host.ends_with(&format!(".{entry}"))) {
                return None;
            }
        }
    }
    Some(proxy)
}

/// An HTTP agent builder for `url`: no redirects, and the environment's
/// proxy (see [`proxy_for`]). A proxy that cannot be used fails closed with
/// `SERVICE_UNAVAILABLE`; the request is never sent directly instead.
pub(crate) fn agent_builder(
    url: &str,
    lookup: &dyn Fn(&str) -> Option<String>,
) -> Result<ureq::AgentBuilder, RunnerError> {
    let builder = ureq::AgentBuilder::new()
        .redirects(0)
        .try_proxy_from_env(false);
    let Some(proxy) = proxy_for(url, lookup) else {
        return Ok(builder);
    };
    let scheme = proxy
        .split_once("://")
        .map(|(scheme, _)| scheme.to_ascii_lowercase());
    if scheme.as_deref().is_some_and(|scheme| scheme != "http") {
        return Err(unavailable());
    }
    let proxy = ureq::Proxy::new(&proxy).map_err(|_| unavailable())?;
    Ok(builder.proxy(proxy))
}

/// The process environment as the Bun build decodes it (lossy UTF-8).
pub(crate) fn process_environment(name: &str) -> Option<String> {
    std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
}

/// The reason of a response body that is not a JSON object under the shared
/// rules.
pub(crate) const RESPONSE_NOT_OBJECT: &str = "the service response is not a valid JSON object";

/// The largest integer both ports represent exactly (`Number.MAX_SAFE_INTEGER`).
pub(crate) const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

/// A service body or event as JSON, with the rules both ports share
/// (`shared/fixtures/transport/json-parse.json`): strict UTF-8 without a
/// byte order mark, no lone surrogate, fewer than 128 nested containers,
/// and no integral number beyond the safe range. `None` is rejected.
#[must_use]
pub(crate) fn parse_json(bytes: &[u8]) -> Option<Value> {
    fn safe(value: &Value) -> bool {
        match value {
            Value::Number(number) => number.as_i64().map_or_else(
                || {
                    number.as_u64().map_or_else(
                        || {
                            number.as_f64().is_some_and(|float| {
                                float.fract() != 0.0 || float.abs() <= 9_007_199_254_740_991.0
                            })
                        },
                        |unsigned| unsigned <= MAX_SAFE_INTEGER,
                    )
                },
                |integer| integer.unsigned_abs() <= MAX_SAFE_INTEGER,
            ),
            Value::Array(items) => items.iter().all(safe),
            Value::Object(object) => object.values().all(safe),
            _ => true,
        }
    }
    // serde_json rejects a byte order mark, a lone surrogate escape and a
    // document nesting 128 containers itself.
    let value: Value = serde_json::from_slice(bytes).ok()?;
    safe(&value).then_some(value)
}

/// A fixture mismatch in a test-seam build.
pub(crate) fn fixture_error(reason: impl Into<String>) -> RunnerError {
    problem(ErrorCode::ServiceProtocolInvalid)
        .with_detail("reason", format!("test fixture: {}", reason.into()))
}

/// The transport: the real network, or the fixture in test-seam builds.
#[derive(Debug)]
pub struct Transport {
    origin: String,
    cancellation: CancellationToken,
    fixture: Option<Fixture>,
    started: Instant,
    /// The process environment (the proxy variables).
    environment: BTreeMap<String, String>,
}

#[derive(Debug)]
struct Fixture {
    document: Value,
    next: usize,
    completed: u64,
    interrupt_after: Option<u64>,
    clock_ms: Option<i64>,
    step_ms: i64,
    ids_used: usize,
}

impl Transport {
    pub fn new(
        system: &SystemContext,
        environment: &Environment,
        cancellation: &CancellationToken,
    ) -> Result<Self, RunnerError> {
        #[allow(unused_mut)]
        let mut transport = Self {
            origin: environment.origin.clone(),
            cancellation: cancellation.clone(),
            fixture: None,
            started: Instant::now(),
            environment: system.environment.clone(),
        };
        #[cfg(feature = "test-seams")]
        if let Some(path) = system.environment.get("PROSE_TEST_SERVICE_FIXTURE") {
            transport.fixture = Some(Fixture::load(path, environment)?);
            transport.observe_completion();
        }
        Ok(transport)
    }

    /// A transport that never reaches the network (unit tests).
    #[cfg(test)]
    pub(crate) fn with_fixture(
        environment: &Environment,
        document: Value,
    ) -> Result<Self, RunnerError> {
        Ok(Self {
            origin: environment.origin.clone(),
            cancellation: CancellationToken::default(),
            fixture: Some(Fixture::parse(document, environment)?),
            started: Instant::now(),
            environment: BTreeMap::new(),
        })
    }

    pub fn is_fixture(&self) -> bool {
        self.fixture.is_some()
    }

    fn check(&self) -> Result<(), RunnerError> {
        if self.cancellation.is_cancelled() {
            Err(problem(ErrorCode::Cancelled))
        } else {
            Ok(())
        }
    }

    fn observe_completion(&mut self) {
        if let Some(fixture) = &self.fixture {
            if fixture.interrupt_after == Some(fixture.completed) {
                self.cancellation.cancel();
            }
        }
    }

    fn complete_exchange(&mut self) {
        if let Some(fixture) = &mut self.fixture {
            fixture.completed += 1;
        }
        self.observe_completion();
    }

    /// Fails when a fixture still holds unused exchanges.
    pub fn finish(&mut self) -> Result<(), RunnerError> {
        if let Some(fixture) = &self.fixture {
            let total = fixture.document["exchanges"].as_array().map_or(0, Vec::len);
            if fixture.next < total {
                return Err(fixture_error(format!(
                    "{} exchange(s) were not requested",
                    total - fixture.next
                )));
            }
        }
        Ok(())
    }

    /// Writes the fixture's preset journal entries.
    pub fn prepare_journal(&self, journal: &super::journal::Journal) -> Result<(), RunnerError> {
        if let Some(entries) = self
            .fixture
            .as_ref()
            .and_then(|fixture| fixture.document["journal"].as_array())
        {
            for entry in entries {
                journal.write_value(entry)?;
            }
        }
        Ok(())
    }

    /// Current time as RFC 3339 with milliseconds.
    pub fn now_rfc3339(&mut self) -> String {
        match self.fixture.as_mut().and_then(Fixture::tick) {
            Some(ms) => chrono::DateTime::from_timestamp_millis(ms)
                .unwrap_or_default()
                .to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            None => chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        }
    }

    /// Monotonic milliseconds since the transport started (virtual with a
    /// fixture clock).
    pub fn monotonic_ms(&mut self) -> u64 {
        match self.fixture.as_mut().and_then(Fixture::tick) {
            Some(ms) => u64::try_from(ms).unwrap_or(0),
            None => u64::try_from(self.started.elapsed().as_millis()).unwrap_or(u64::MAX),
        }
    }

    /// A `UUIDv4`: the fixture's next `ids` entry, or a random one.
    pub fn uuid_v4(&mut self) -> Result<String, RunnerError> {
        if let Some(fixture) = &mut self.fixture {
            let value = fixture.document["ids"][fixture.ids_used]
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| fixture_error("no deterministic id left in `ids`"))?;
            fixture.ids_used += 1;
            return Ok(value);
        }
        Ok(uuid::Uuid::new_v4().to_string())
    }

    /// The stored credential of this service, from the OS credential store.
    pub fn stored_credential(
        &mut self,
        environment: &Environment,
    ) -> Result<Option<String>, RunnerError> {
        self.check()?;
        if let Some(fixture) = &self.fixture {
            if fixture.document["storeAvailable"] == false {
                return Err(problem(ErrorCode::CredentialStoreUnavailable));
            }
            let slot = if fixture.document.get("credentials").is_some() {
                &fixture.document["credentials"][environment.name]
            } else {
                &fixture.document["credential"]
            };
            return Ok(slot.as_str().map(str::to_owned));
        }
        crate::service_account::native_store("get", None, &self.cancellation, environment)
    }

    fn next_exchange(
        &mut self,
        request: &Request,
        token: Option<&str>,
    ) -> Result<Value, RunnerError> {
        let origin = self.origin.clone();
        let fixture = self.fixture.as_mut().expect("fixture transport");
        let index = fixture.next;
        let exchange = fixture.document["exchanges"]
            .get(index)
            .cloned()
            .ok_or_else(|| {
                fixture_error(format!(
                    "unexpected request {} {} (no exchange left)",
                    request.method, request.path
                ))
            })?;
        fixture.next += 1;
        let place = format!("exchange {index}");
        if exchange["method"] != request.method.as_str()
            || exchange["path"] != request.path.as_str()
        {
            return Err(fixture_error(format!(
                "{place} expects {} {} but the product sent {} {}",
                exchange["method"].as_str().unwrap_or("?"),
                exchange["path"].as_str().unwrap_or("?"),
                request.method,
                request.path
            )));
        }
        if exchange
            .get("origin")
            .is_some_and(|value| value != origin.as_str())
        {
            return Err(fixture_error(format!("{place} origin differs")));
        }
        let expected_query = exchange["query"].as_object().cloned().unwrap_or_default();
        let sent_query = request
            .query
            .iter()
            .map(|(name, value)| (name.clone(), Value::String(value.clone())))
            .collect::<Map<_, _>>();
        if expected_query != sent_query || sent_query.len() != request.query.len() {
            return Err(fixture_error(format!("{place} query differs")));
        }
        let headers = request.wire_headers(token);
        if let Some(assertions) = exchange["requestHeaders"].as_object() {
            for (name, expected) in assertions {
                let actual = headers
                    .iter()
                    .find(|(key, _)| key.eq_ignore_ascii_case(name))
                    .map(|(_, value)| value.as_str());
                let ok = match expected {
                    Value::String(text) => actual == Some(text.as_str()),
                    Value::Object(object) if object.contains_key("present") => {
                        actual.is_some() == (object["present"] == true)
                    }
                    Value::Object(object) => match (object["pattern"].as_str(), actual) {
                        (Some(pattern), Some(value)) => {
                            super::render::pattern_matches(pattern, value).ok_or_else(|| {
                                fixture_error(format!(
                                    "{place} header pattern for {name} is not supported"
                                ))
                            })?
                        }
                        _ => false,
                    },
                    _ => false,
                };
                if !ok {
                    return Err(fixture_error(format!(
                        "{place} request header {name} differs"
                    )));
                }
            }
        }
        let sent = request.body.clone().unwrap_or_default();
        if let Some(expected) = exchange.get("expectedBody") {
            let parsed: Option<Value> = serde_json::from_slice(&sent).ok();
            if parsed.as_ref() != Some(expected) {
                return Err(fixture_error(format!("{place} request body differs")));
            }
        }
        if let Some(expected) = exchange["expectedBodyText"].as_str() {
            if sent != expected.as_bytes() {
                return Err(fixture_error(format!("{place} request body text differs")));
            }
        }
        if let Some(expected) = exchange["expectedSha256"].as_str() {
            if format!("{:x}", Sha256::digest(&sent)) != expected {
                return Err(fixture_error(format!(
                    "{place} request body digest differs"
                )));
            }
        }
        Ok(exchange)
    }

    fn fixture_headers(exchange: &Value) -> BTreeMap<String, String> {
        let mut headers = exchange["responseHeaders"]
            .as_object()
            .map(|object| {
                object
                    .iter()
                    .filter_map(|(name, value)| {
                        Some((name.to_ascii_lowercase(), value.as_str()?.to_owned()))
                    })
                    .collect::<BTreeMap<_, _>>()
            })
            .unwrap_or_default();
        if let Some(location) = exchange["redirect"].as_str() {
            headers.insert("location".into(), location.to_owned());
        }
        headers
    }

    fn fixture_body(exchange: &Value) -> Result<Vec<u8>, RunnerError> {
        if let Some(text) = exchange["bodyText"].as_str() {
            return Ok(text.as_bytes().to_vec());
        }
        if let Some(encoded) = exchange["bodyBase64"].as_str() {
            return base64_decode(encoded).ok_or_else(|| fixture_error("bodyBase64 is not base64"));
        }
        if exchange.get("sse").is_some() {
            return Ok(sse::fixture_bytes(&exchange["sse"]["frames"]).concat());
        }
        Ok(exchange
            .get("body")
            .map(|value| serde_json::to_vec(value).expect("JSON serializes"))
            .unwrap_or_default())
    }

    fn fixture_status(exchange: &Value) -> Result<u16, RunnerError> {
        exchange["status"]
            .as_u64()
            .and_then(|value| u16::try_from(value).ok())
            .filter(|value| (100..=599).contains(value))
            .ok_or_else(|| fixture_error("status must be 100-599"))
    }

    /// Sends a buffered request; any status is returned.
    pub fn send(
        &mut self,
        environment: &Environment,
        request: &Request,
        token: Option<&str>,
    ) -> Result<Response, RunnerError> {
        let _ = environment;
        self.check()?;
        let limit = request.class.max_response_bytes();
        if self.fixture.is_some() {
            let exchange = self.next_exchange(request, token)?;
            let result = (|| {
                if exchange["disconnect"].is_string() {
                    return Err(unavailable());
                }
                let status = Self::fixture_status(&exchange)?;
                let body = Self::fixture_body(&exchange)?;
                if exchange.get("sse").is_some() && exchange["sse"]["end"] != "close" {
                    return Err(unavailable());
                }
                Ok((status, Self::fixture_headers(&exchange), body))
            })();
            self.complete_exchange();
            self.check()?;
            let (status, headers, body) = result?;
            return bounded_response(status, headers, body, limit);
        }
        let response = self.real_call(request, token, Some(request.class.timeout()), None)?;
        let (status, headers) = response_head(&response);
        let mut body = Vec::new();
        let read = response
            .into_reader()
            .take(limit + 1)
            .read_to_end(&mut body);
        self.check()?;
        read.map_err(|_| unavailable())?;
        bounded_response(status, headers, body, limit)
    }

    fn real_call(
        &self,
        request: &Request,
        token: Option<&str>,
        timeout: Option<Duration>,
        stream: Option<(Duration, Duration)>,
    ) -> Result<ureq::Response, RunnerError> {
        let url = request.url(&self.origin);
        let environment = &self.environment;
        let mut builder = agent_builder(&url, &|name| environment.get(name).cloned())?;
        if let Some(timeout) = timeout {
            builder = builder.timeout(timeout);
        }
        if let Some((connect, idle)) = stream {
            builder = builder.timeout_connect(connect).timeout_read(idle);
        }
        let agent = builder.build();
        let mut call = agent.request(&request.method, &url);
        for (name, value) in request.wire_headers(token) {
            call = call.set(&name, &value);
        }
        // The exchange runs on its own thread, so cancellation ends the wait
        // at once (as the Bun build's AbortSignal does), and a stream or
        // download waits at most `connect` for the response headers.
        let body = request.body.clone();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let result = match body {
                Some(body) => call.send_bytes(&body),
                None => call.call(),
            };
            let _ = sender.send(result);
        });
        let headers_by = stream.map(|(connect, _)| Instant::now() + connect);
        let result = loop {
            self.check()?;
            if headers_by.is_some_and(|deadline| Instant::now() >= deadline) {
                return Err(unavailable());
            }
            match receiver.recv_timeout(Duration::from_millis(20)) {
                Ok(result) => break result,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return Err(unavailable()),
            }
        };
        self.check()?;
        match result {
            Ok(response) | Err(ureq::Error::Status(_, response)) => Ok(response),
            Err(ureq::Error::Transport(_)) => Err(unavailable()),
        }
    }

    /// Opens a server-sent event stream. Transport failure before response
    /// headers is reported as [`StreamOpen::Dropped`] so a caller can decide
    /// whether to resubmit.
    pub fn open_stream(
        &mut self,
        environment: &Environment,
        request: &Request,
        token: Option<&str>,
    ) -> Result<StreamOpen, RunnerError> {
        let _ = environment;
        self.check()?;
        let class = TransportClass::Stream;
        if self.fixture.is_some() {
            let exchange = self.next_exchange(request, token)?;
            if exchange["disconnect"] == "before-response" {
                self.complete_exchange();
                self.check()?;
                return Ok(StreamOpen::Dropped);
            }
            let status = Self::fixture_status(&exchange)?;
            let headers = Self::fixture_headers(&exchange);
            if !(200..300).contains(&status) || exchange.get("sse").is_none() {
                let body = Self::fixture_body(&exchange)?;
                self.complete_exchange();
                self.check()?;
                if exchange["disconnect"].is_string() {
                    return Err(unavailable());
                }
                return Ok(StreamOpen::Response(bounded_response(
                    status,
                    headers,
                    body,
                    class.max_response_bytes(),
                )?));
            }
            let frames = if exchange["disconnect"] == "after-headers" {
                Vec::new()
            } else {
                sse::fixture_bytes(&exchange["sse"]["frames"])
            };
            let end = match (
                exchange["disconnect"].as_str(),
                exchange["sse"]["end"].as_str(),
            ) {
                (Some(_), _) | (_, Some("disconnect")) => sse::StreamEnd::Disconnected,
                (_, Some("idle")) => sse::StreamEnd::Idle,
                _ => sse::StreamEnd::Closed,
            };
            let fixture = self.fixture.as_mut().expect("fixture");
            let completion = FixtureCompletion {
                cancellation: self.cancellation.clone(),
                fire: fixture.interrupt_after == Some(fixture.completed + 1),
            };
            fixture.completed += 1;
            let reader = sse::SseReader::new(
                Box::new(sse::FixtureSource::new(frames, end, completion.into_hook())),
                self.cancellation.clone(),
                TransportClass::max_event_bytes(),
            );
            return Ok(StreamOpen::Events {
                status,
                headers,
                reader,
            });
        }
        let response = match self.real_call(
            request,
            token,
            None,
            Some((class.connect_timeout(), class.idle_timeout())),
        ) {
            Ok(response) => response,
            Err(error) if error.code == ErrorCode::ServiceUnavailable => {
                return Ok(StreamOpen::Dropped);
            }
            Err(error) => return Err(error),
        };
        let (status, headers) = response_head(&response);
        if !(200..300).contains(&status) {
            let limit = class.max_response_bytes();
            let mut body = Vec::new();
            let read = response
                .into_reader()
                .take(limit + 1)
                .read_to_end(&mut body);
            self.check()?;
            read.map_err(|_| unavailable())?;
            return Ok(StreamOpen::Response(bounded_response(
                status, headers, body, limit,
            )?));
        }
        let reader = sse::SseReader::new(
            Box::new(sse::ThreadSource::spawn(
                response.into_reader(),
                class.idle_timeout(),
            )),
            self.cancellation.clone(),
            TransportClass::max_event_bytes(),
        );
        Ok(StreamOpen::Events {
            status,
            headers,
            reader,
        })
    }

    /// Streams a 2xx body into `sink`, at most `max_bytes`. A non-2xx
    /// response is classified.
    pub fn download(
        &mut self,
        environment: &Environment,
        request: &Request,
        token: Option<&str>,
        sink: &mut dyn Write,
        max_bytes: u64,
        contract: &str,
    ) -> Result<Downloaded, RunnerError> {
        let _ = environment;
        self.check()?;
        let control = TransportClass::Control.max_response_bytes();
        let (status, headers, mut reader): (u16, BTreeMap<String, String>, Box<dyn Read>) =
            if self.fixture.is_some() {
                let exchange = self.next_exchange(request, token)?;
                let status = Self::fixture_status(&exchange);
                let body = Self::fixture_body(&exchange);
                self.complete_exchange();
                self.check()?;
                if exchange["disconnect"] == "before-response"
                    || exchange["disconnect"] == "after-headers"
                {
                    return Err(unavailable());
                }
                let (status, body) = (status?, body?);
                let failing = exchange["disconnect"] == "mid-body";
                let reader: Box<dyn Read> = if failing {
                    let half = body.len() / 2;
                    Box::new(std::io::Cursor::new(body[..half].to_vec()).chain(FailingReader))
                } else {
                    Box::new(std::io::Cursor::new(body))
                };
                (status, Self::fixture_headers(&exchange), reader)
            } else {
                let class = TransportClass::Download;
                let response = self.real_call(
                    request,
                    token,
                    None,
                    Some((class.connect_timeout(), class.idle_timeout())),
                )?;
                let (status, headers) = response_head(&response);
                (status, headers, Box::new(response.into_reader()))
            };
        if !(200..300).contains(&status) {
            let mut body = Vec::new();
            let read = reader.take(control + 1).read_to_end(&mut body);
            self.check()?;
            read.map_err(|_| unavailable())?;
            let response = Response {
                status,
                headers,
                body: if body.len() as u64 > control {
                    Vec::new()
                } else {
                    body
                },
            };
            return Err(classify(contract, request, &response, token));
        }
        let mut digest = Sha256::new();
        let mut total: u64 = 0;
        let mut buffer = vec![0_u8; 64 * 1024];
        loop {
            self.check()?;
            let count = reader.read(&mut buffer).map_err(|_| unavailable())?;
            if count == 0 {
                break;
            }
            total += count as u64;
            if total > max_bytes {
                return Err(problem(ErrorCode::ServiceResponseTooLarge));
            }
            digest.update(&buffer[..count]);
            sink.write_all(&buffer[..count])
                .map_err(|_| RunnerError::invocation("cannot write the downloaded file"))?;
        }
        Ok(Downloaded {
            status,
            headers,
            bytes: total,
            sha256: format!("{:x}", digest.finalize()),
        })
    }
}

#[derive(Debug)]
struct FailingReader;

impl Read for FailingReader {
    fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
        Err(std::io::Error::other("fixture disconnect"))
    }
}

struct FixtureCompletion {
    cancellation: CancellationToken,
    fire: bool,
}

impl FixtureCompletion {
    fn into_hook(self) -> Box<dyn FnMut() + Send> {
        Box::new(move || {
            if self.fire {
                self.cancellation.cancel();
            }
        })
    }
}

impl Fixture {
    #[cfg(feature = "test-seams")]
    fn load(path: &str, environment: &Environment) -> Result<Self, RunnerError> {
        const LIMIT: u64 = 32 * 1024 * 1024;
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .and_then(|file| file.take(LIMIT + 1).read_to_end(&mut bytes))
            .map_err(|_| fixture_error("cannot read PROSE_TEST_SERVICE_FIXTURE"))?;
        if bytes.len() as u64 > LIMIT {
            return Err(fixture_error("larger than 32 MiB"));
        }
        let document = serde_json::from_slice(&bytes).map_err(|_| fixture_error("not JSON"))?;
        Self::parse(document, environment)
    }

    #[cfg_attr(not(any(test, feature = "test-seams")), allow(dead_code))]
    fn parse(document: Value, environment: &Environment) -> Result<Self, RunnerError> {
        let exchanges_ok = document
            .get("exchanges")
            .is_none_or(|exchanges| exchanges.as_array().is_some_and(|list| list.len() <= 512));
        if !document.is_object() || !exchanges_ok {
            return Err(fixture_error(
                "must be an object with at most 512 exchanges",
            ));
        }
        if document
            .get("environment")
            .is_some_and(|value| value != environment.name)
        {
            return Err(fixture_error("environment differs from the service"));
        }
        let clock_ms = match document["clock"]["start"].as_str() {
            Some(start) => Some(
                chrono::DateTime::parse_from_rfc3339(start)
                    .map_err(|_| fixture_error("clock.start is not RFC 3339"))?
                    .timestamp_millis(),
            ),
            None => None,
        };
        Ok(Self {
            step_ms: document["clock"]["stepMs"].as_i64().unwrap_or(0),
            interrupt_after: document["interruptAfterExchange"].as_u64(),
            document,
            next: 0,
            completed: 0,
            clock_ms,
            ids_used: 0,
        })
    }

    fn tick(&mut self) -> Option<i64> {
        let now = self.clock_ms?;
        self.clock_ms = Some(now + self.step_ms);
        Some(now)
    }
}

fn response_head(response: &ureq::Response) -> (u16, BTreeMap<String, String>) {
    let headers = response
        .headers_names()
        .into_iter()
        .filter_map(|name| {
            let value = response.header(&name)?.to_owned();
            Some((name.to_ascii_lowercase(), value))
        })
        .collect();
    (response.status(), headers)
}

fn bounded_response(
    status: u16,
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
    limit: u64,
) -> Result<Response, RunnerError> {
    if body.len() as u64 > limit {
        if (200..300).contains(&status) {
            return Err(problem(ErrorCode::ServiceResponseTooLarge));
        }
        // An oversize error body is classified by status alone.
        return Ok(Response {
            status,
            headers,
            body: Vec::new(),
        });
    }
    Ok(Response {
        status,
        headers,
        body,
    })
}

/// Classifies a non-2xx response: body `code`, then route override, then
/// status (the manifest `errorClassification`).
pub fn classify(
    contract: &str,
    request: &Request,
    response: &Response,
    credential: Option<&str>,
) -> RunnerError {
    let table = &manifest()["errorClassification"];
    let body = match parse_json(&response.body) {
        Some(Value::Object(object)) => Some(object),
        _ => None,
    };
    let body_code = body
        .as_ref()
        .and_then(|object| object.get("code"))
        .and_then(Value::as_str);
    let status = response.status;
    let by_body = body_code.and_then(|code| table["bodyCodes"][code].as_str());
    let by_route = || {
        table["routeOverrides"].as_array().and_then(|overrides| {
            overrides.iter().find_map(|entry| {
                (entry["code"] != "RUN_SUBMISSION_AMBIGUOUS"
                    && entry["method"] == request.method.as_str()
                    && entry["path"] == request.template.as_str()
                    && entry["status"] == u64::from(status))
                .then(|| entry["code"].as_str())
                .flatten()
            })
        })
    };
    let by_status = || {
        table["statuses"][status.to_string()]
            .as_str()
            .or_else(|| {
                (500..=599)
                    .contains(&status)
                    .then(|| table["statuses"]["5xx"].as_str())
                    .flatten()
            })
            .or_else(|| table["otherStatus"].as_str())
    };
    let code = by_body
        .or_else(by_route)
        .or_else(by_status)
        .and_then(ErrorCode::from_code)
        .unwrap_or(ErrorCode::ServiceUnavailable);
    let mut error = RunnerError::catalog(code).with_detail("serviceStatus", status);
    // A disabled capability names no internal feature: the body's feature
    // name and error text are never copied (`errorClassification.featureDisabled`).
    let feature_disabled = body_code == Some("feature_disabled");
    if let Some(service_code) = body_code.filter(|code| table["bodyCodes"].get(*code).is_some()) {
        error = error.with_detail("serviceCode", service_code);
    }
    let frozen = table["frozenContracts"]
        .as_array()
        .is_some_and(|contracts| contracts.iter().any(|value| value == contract));
    let message_statuses = table["serviceMessage"]["statuses"].as_array();
    if !frozen
        && !feature_disabled
        && message_statuses
            .is_some_and(|statuses| statuses.iter().any(|value| value == u64::from(status)))
    {
        if let Some(message) = body
            .as_ref()
            .and_then(|object| object.get("error"))
            .and_then(Value::as_str)
            .and_then(|text| render::sanitize_service_message(text, credential))
        {
            error = error.with_detail("serviceMessage", message);
        }
    }
    error
}

/// Percent-encodes one path segment (RFC 3986 unreserved characters stay).
pub fn encode_segment(value: &str) -> String {
    encode(value)
}

/// Percent-encodes a query name or value.
pub fn encode_query(value: &str) -> String {
    encode(value)
}

fn encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(byte));
        } else {
            let _ = write!(encoded, "%{byte:02X}");
        }
    }
    encoded
}

/// Standard base64 (RFC 4648) with optional padding.
pub fn base64_decode(text: &str) -> Option<Vec<u8>> {
    let mut output = Vec::with_capacity(text.len() * 3 / 4);
    let mut buffer = 0_u32;
    let mut bits = 0;
    let trimmed = text.trim_end_matches('=');
    if text.len() - trimmed.len() > 2 {
        return None;
    }
    for byte in trimmed.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        };
        buffer = (buffer << 6) | u32::from(value);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            output.push(u8::try_from((buffer >> bits) & 0xff).ok()?);
        }
    }
    Some(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn production() -> Environment {
        Environment::production()
    }

    fn operation(id: &str) -> &'static Value {
        super::super::operation(id).unwrap()
    }

    #[test]
    fn proxy_selection_matches_the_shared_vectors() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/transport/proxy-selection.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            let environment: BTreeMap<String, String> = case["environment"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(name, value)| {
                    (
                        name.clone(),
                        value.as_str().unwrap().replace("{PORT}", "1234"),
                    )
                })
                .collect();
            let selected = proxy_for(case["url"].as_str().unwrap(), &|name| {
                environment.get(name).cloned()
            });
            let expected = (case["proxied"] == true).then(|| "http://127.0.0.1:1234".to_owned());
            assert_eq!(selected, expected, "{}", case["id"]);
        }
        // A proxy that cannot be used fails closed, never direct.
        for proxy in ["socks5://127.0.0.1:1", "https://127.0.0.1:1", "ftp://x"] {
            let lookup = |name: &str| (name == "HTTPS_PROXY").then(|| proxy.to_owned());
            assert_eq!(
                agent_builder("https://example.invalid/", &lookup)
                    .unwrap_err()
                    .code,
                ErrorCode::ServiceUnavailable,
                "{proxy}"
            );
        }
    }

    #[test]
    fn service_json_parsing_matches_the_shared_vectors() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/transport/json-parse.json"
        ))
        .unwrap();
        assert_eq!(fixture["maxSafeInteger"], MAX_SAFE_INTEGER);
        for case in fixture["cases"].as_array().unwrap() {
            let bytes = case["text"].as_str().map_or_else(
                || {
                    let hex = case["bytesHex"].as_str().unwrap();
                    (0..hex.len())
                        .step_by(2)
                        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                        .collect()
                },
                |text| text.as_bytes().to_vec(),
            );
            assert_eq!(
                parse_json(&bytes).is_some(),
                case["accepted"] == true,
                "{}",
                case["id"]
            );
        }
    }

    /// A local server that accepts connections and never answers.
    fn silent_server() -> (String, std::net::TcpListener) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        (origin, listener)
    }

    fn real(origin: String, cancellation: &CancellationToken) -> Transport {
        Transport {
            origin,
            cancellation: cancellation.clone(),
            fixture: None,
            started: Instant::now(),
            environment: BTreeMap::new(),
        }
    }

    fn get_request() -> Request {
        Request::from_manifest(operation("wallet.balance"), 0, "/wallet")
    }

    #[test]
    fn real_call_waits_at_most_the_header_deadline_for_a_stream() {
        let (origin, _listener) = silent_server();
        let transport = real(origin, &CancellationToken::default());
        let began = Instant::now();
        let error = transport
            .real_call(
                &get_request(),
                None,
                None,
                Some((Duration::from_millis(300), Duration::from_secs(45))),
            )
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::ServiceUnavailable);
        assert!(
            began.elapsed() < Duration::from_secs(5),
            "{:?}",
            began.elapsed()
        );
    }

    #[test]
    fn real_call_is_cancellable_mid_request() {
        let (origin, _listener) = silent_server();
        let cancellation = CancellationToken::default();
        let transport = real(origin, &cancellation);
        let trigger = cancellation.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(200));
            trigger.cancel();
        });
        let began = Instant::now();
        let error = transport
            .real_call(&get_request(), None, Some(Duration::from_secs(30)), None)
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::Cancelled);
        assert!(
            began.elapsed() < Duration::from_secs(5),
            "{:?}",
            began.elapsed()
        );
    }

    fn response(status: u16, body: &str) -> Response {
        Response {
            status,
            headers: BTreeMap::new(),
            body: body.as_bytes().to_vec(),
        }
    }

    #[test]
    fn body_code_precedes_route_and_status() {
        let request = Request::from_manifest(operation("repo.list"), 0, "/repos");
        for status in [400, 403, 404, 503] {
            let error = classify(
                "service/1",
                &request,
                &response(
                    status,
                    r#"{"error":"feature some_flag is off","code":"feature_disabled","feature":"some_flag"}"#,
                ),
                None,
            );
            assert_eq!(error.code, ErrorCode::ServiceFeatureDisabled, "{status}");
            let details = error.details.unwrap();
            // Only the status and code: never the internal feature name
            // or the service's text about it.
            assert_eq!(details["serviceStatus"], status);
            assert_eq!(details["serviceCode"], "feature_disabled");
            assert_eq!(details.len(), 2, "{details:?}");
        }
        let error = classify(
            "service/1",
            &request,
            &response(401, r#"{"error":"Reconnect GitHub"}"#),
            None,
        );
        assert_eq!(error.code, ErrorCode::GithubLinkRequired);
        let error = classify("service/1", &request, &response(404, "Not found"), None);
        assert_eq!(error.code, ErrorCode::ServiceResourceNotFound);
        let error = classify("service/1", &request, &response(302, ""), None);
        assert_eq!(error.code, ErrorCode::ServiceUnavailable);
    }

    #[test]
    fn service_message_is_sanitized_and_limited_to_rejections() {
        let request = Request::from_manifest(operation("program.save"), 0, "/programs/x");
        let key = "rr_test_0123456789abcdef0123456789abcdef";
        let error = classify(
            "service/1",
            &request,
            &response(400, &format!(r#"{{"error":"bad\nslug {key}"}}"#)),
            Some(key),
        );
        let details = error.details.unwrap();
        assert_eq!(details["serviceMessage"], "bad slug [REDACTED]");
        let error = classify(
            "service/1",
            &request,
            &response(500, r#"{"error":"boom"}"#),
            None,
        );
        assert!(error.details.unwrap().get("serviceMessage").is_none());
        let error = classify(
            "account/1",
            &request,
            &response(400, r#"{"error":"x"}"#),
            None,
        );
        assert!(error.details.unwrap().get("serviceMessage").is_none());
    }

    #[test]
    fn fixture_asserts_query_headers_and_consumption() {
        let environment = production();
        let mut transport = Transport::with_fixture(
            &environment,
            json!({"exchanges":[{"method":"GET","path":"/runs","query":{"limit":"2"},
                "requestHeaders":{"X-OpenProse-Client":{"pattern":"^cli/[0-9.]+\\+(rust|bun)$"},"Authorization":"Bearer t"},
                "status":200,"body":{"runs":[]}}]}),
        )
        .unwrap();
        let request = Request::from_manifest(operation("run.list"), 0, "/runs").query("limit", "2");
        let response = transport.send(&environment, &request, Some("t")).unwrap();
        assert_eq!(response.json_object().unwrap()["runs"], json!([]));
        assert!(transport.finish().is_ok());
        let mut transport = Transport::with_fixture(
            &environment,
            json!({"exchanges":[{"method":"GET","path":"/runs","status":200,"body":{}}]}),
        )
        .unwrap();
        assert!(transport.finish().is_err());
        assert!(transport.send(&environment, &request, None).is_err());
    }

    #[test]
    fn oversize_success_is_too_large() {
        let environment = production();
        let big = "x".repeat(1024 * 1024 + 1);
        let mut transport = Transport::with_fixture(
            &environment,
            json!({"exchanges":[{"method":"GET","path":"/health","status":200,"bodyText":big}]}),
        )
        .unwrap();
        let request = Request::from_manifest(operation("service.status"), 0, "/health");
        assert_eq!(
            transport
                .send(&environment, &request, None)
                .unwrap_err()
                .code,
            ErrorCode::ServiceResponseTooLarge
        );
    }

    #[test]
    fn encodes_segments_and_base64() {
        assert_eq!(encode_segment("a b/c"), "a%20b%2Fc");
        assert_eq!(base64_decode("aGVsbG8=").unwrap(), b"hello");
        assert!(base64_decode("***").is_none());
    }
}
