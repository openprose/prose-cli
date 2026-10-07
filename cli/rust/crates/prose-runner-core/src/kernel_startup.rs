//! Bounded published kernel acquisition. Payload text remains opaque.
use crate::error::{ErrorCode, RunnerError};
use crate::image::{RuntimeImage, sha256_hex};
use serde_json::{Value, json};
use std::io::Read;
use std::time::{Duration, Instant};

const POLICY: &str = include_str!("../../../../shared/image/kernel-startup/policy.json");
const TEMPLATE: &str =
    include_str!("../../../../shared/image/kernel-startup/manifest.template.json");
const TASK: &[u8] =
    include_bytes!("../../../../shared/image/kernel-startup/contracts/task-envelope.schema.json");
const TERMINAL: &[u8] =
    include_bytes!("../../../../shared/image/kernel-startup/contracts/terminal.schema.json");
const FRAMING: &[u8] =
    include_bytes!("../../../../shared/image/kernel-startup/contracts/framing.txt");

pub const PUBLISHED_KERNEL_STARTUP: bool = env!("OPENPROSE_KERNEL_STARTUP").as_bytes()[0] == b'p';

#[derive(Debug)]
pub struct KernelResponse {
    pub status: u16,
    pub location: Option<String>,
    pub bytes: Vec<u8>,
}
fn invalid(reason: &str) -> RunnerError {
    RunnerError::catalog(ErrorCode::ImageInvalid).with_detail("reason", reason)
}
fn retrieval(stage: &str, status: Option<u16>) -> RunnerError {
    if !["entry", "descriptor", "inventory", "kernel"].contains(&stage)
        || status.is_some_and(|value| {
            !(100..=599).contains(&value) || value == 200 || (stage == "entry" && value < 400)
        })
    {
        return invalid(UNVERIFIED);
    }
    let mut error = RunnerError::catalog(ErrorCode::KernelRetrievalFailed)
        .with_detail("stage", stage)
        .with_detail("origin", "https://pkg.prose.md")
        .with_detail(
            "failureKind",
            if status.is_some() {
                "http"
            } else {
                "transport"
            },
        )
        .with_detail(
            "reason",
            if status.is_some() {
                "Published kernel HTTP request failed."
            } else {
                "Published kernel transport or response read failed."
            },
        );
    error.retryable = status.is_none_or(|value| [408, 429, 500, 502, 503, 504].contains(&value));
    if let Some(value) = status {
        error = error.with_detail("httpStatus", value);
    }
    error
}
// Transport is staged at the request call site, never inferred from an external URL.
fn transport() -> RunnerError {
    RunnerError::catalog(ErrorCode::KernelRetrievalFailed)
}
fn staged(error: RunnerError, stage: &str) -> RunnerError {
    match error.code {
        ErrorCode::ImageInvalid
        | ErrorCode::ImageTooLarge
        | ErrorCode::Cancelled
        | ErrorCode::StartupTimeout => error,
        _ => retrieval(stage, None),
    }
}
fn hash_valid(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The reason of a kernel retrieval that was cancelled or ran past its
/// startup deadline (the same text in both ports).
const INTERRUPTED: &str = "Kernel retrieval was cancelled or exceeded its startup deadline.";
/// The reason of any other retrieval or verification failure without its own.
const UNVERIFIED: &str = "Cannot retrieve or verify the published kernel; no fallback was used.";

/// Fetch with a single total startup deadline and no redirects or retries beyond
/// the one explicitly verified moving-entry redirect. No credentials are sent.
///
/// Each request runs on its own thread, so cancellation (`CANCELLED`) and the
/// deadline (`STARTUP_TIMEOUT`) end the wait at once, also mid-request.
///
/// # Errors
/// Returns a startup, image or transport error if retrieval is cancelled,
/// exceeds a bound, or fails the pinned identity and content checks.
///
/// # Panics
/// Panics if compile-time policy or template JSON is invalid. Shared contract
/// tests validate these repository-owned inputs before source admission.
pub fn published_kernel(
    cancellation: &crate::CancellationToken,
) -> Result<RuntimeImage, RunnerError> {
    let policy: Value = serde_json::from_str(POLICY).expect("kernel policy");
    let deadline =
        Instant::now() + Duration::from_millis(policy["timeoutMs"].as_u64().expect("timeout"));
    let agent = |url: &str| {
        crate::service::http::agent_builder(url, &crate::service::http::process_environment)
            .map(ureq::AgentBuilder::build)
            .map_err(|_| transport())
    };
    let user_agent = policy["userAgent"].as_str().expect("user agent").to_owned();
    let interrupted = || {
        let code = if cancellation.is_cancelled() {
            ErrorCode::Cancelled
        } else {
            ErrorCode::StartupTimeout
        };
        RunnerError::catalog(code).with_detail("reason", INTERRUPTED)
    };
    let result = published_kernel_with(|url, limit| {
        if cancellation.is_cancelled() {
            return Err(interrupted());
        }
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or_else(interrupted)?;
        let (sender, receiver) = std::sync::mpsc::channel();
        let request = agent(url)?
            .get(url)
            .set("User-Agent", &user_agent)
            .timeout(remaining);
        std::thread::spawn(move || {
            let _ = sender.send(fetch(request, limit));
        });
        loop {
            if cancellation.is_cancelled() || Instant::now() >= deadline {
                return Err(interrupted());
            }
            match receiver.recv_timeout(Duration::from_millis(20)) {
                Ok(outcome) => return outcome,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(transport());
                }
            }
        }
    });
    result.map_err(|error| {
        if cancellation.is_cancelled() || Instant::now() >= deadline {
            interrupted()
        } else {
            error
        }
    })
}

/// One kernel request: the status, the redirect location and (for 200) at
/// most `limit` body bytes.
fn fetch(request: ureq::Request, limit: usize) -> Result<KernelResponse, RunnerError> {
    let response = match request.call() {
        Ok(response) | Err(ureq::Error::Status(_, response)) => response,
        Err(ureq::Error::Transport(_)) => return Err(transport()),
    };
    let status = response.status();
    let location = response.header("Location").map(str::to_owned);
    let mut bytes = Vec::new();
    if status == 200 {
        response
            .into_reader()
            .take((limit + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| transport())?;
        if bytes.len() > limit {
            return Err(too_large(limit));
        }
    }
    Ok(KernelResponse {
        status,
        location,
        bytes,
    })
}

fn too_large(limit: usize) -> RunnerError {
    RunnerError::catalog(ErrorCode::ImageTooLarge).with_detail("maximumBytes", limit)
}

/// Provider-free acquisition seam; callers supply HTTP observations, not prose semantics.
///
/// # Errors
/// Returns a startup, image or transport error if retrieval is cancelled,
/// exceeds a bound, or fails the pinned identity and content checks.
///
/// # Panics
/// Panics if compile-time policy or template JSON is invalid. Shared contract
/// tests validate these repository-owned inputs before source admission.
pub fn published_kernel_with(
    mut get: impl FnMut(&str, usize) -> Result<KernelResponse, RunnerError>,
) -> Result<RuntimeImage, RunnerError> {
    let policy: Value = serde_json::from_str(POLICY).expect("kernel policy");
    let entry = policy["entry"].as_str().expect("entry");
    let origin = policy["origin"].as_str().expect("origin");
    let metadata_limit = usize::try_from(policy["maxMetadataBytes"].as_u64().expect("limit"))
        .map_err(|_| invalid("Kernel metadata limit exceeds this platform"))?;
    let response = get(entry, metadata_limit).map_err(|error| staged(error, "entry"))?;
    if (400..=599).contains(&response.status) {
        return Err(retrieval("entry", Some(response.status)));
    }
    let location = response
        .location
        .filter(|location| {
            [301, 302, 303, 307, 308].contains(&response.status) && !location.is_empty()
        })
        .ok_or_else(|| invalid("Kernel entry must redirect to an immutable release."))?;
    let url = if location.starts_with('/') && !location.starts_with("//") {
        format!("{origin}{location}")
    } else {
        location
    };
    let prefix = format!("{origin}/releases/");
    let release = url
        .strip_prefix(&prefix)
        .and_then(|s| s.strip_suffix("/core/README.md"))
        .filter(|_| !url.contains(['%', '?', '#']))
        .ok_or_else(|| invalid("Kernel entry selected an unsupported release URL."))?;
    if release.is_empty()
        || release.len() > 128
        || !release.as_bytes()[0].is_ascii_alphanumeric()
        || !release
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return Err(invalid("Kernel entry selected an unsupported release URL."));
    }
    let root = url.strip_suffix("README.md").expect("verified suffix");
    let mut read = |url: &str, limit: usize, stage: &str| -> Result<Vec<u8>, RunnerError> {
        let response = get(url, limit).map_err(|error| staged(error, stage))?;
        if response.status != 200 {
            return Err(retrieval(stage, Some(response.status)));
        }
        if response.bytes.len() > limit {
            return Err(too_large(limit));
        }
        Ok(response.bytes)
    };
    let descriptor: Value = serde_json::from_slice(&read(
        &format!("{root}descriptor.json"),
        metadata_limit,
        "descriptor",
    )?)
    .map_err(|_| invalid(UNVERIFIED))?;
    let commit = descriptor["source"]["commit"].as_str().unwrap_or("");
    let inventory_hash = descriptor["inventory_sha256"].as_str().unwrap_or("");
    if descriptor["identity"] != "openprose/core"
        || descriptor["release"] != release
        || descriptor["exports"]["entry"] != "README.md"
        || descriptor["inventory"] != format!("releases/{release}/core/inventory.json")
        || !hash_valid(commit, 40)
        || !hash_valid(inventory_hash, 64)
    {
        return Err(invalid("Published kernel descriptor identity is invalid."));
    }
    let inventory = read(
        &format!("{root}inventory.json"),
        metadata_limit,
        "inventory",
    )?;
    if sha256_hex(&inventory) != inventory_hash {
        return Err(invalid("Published kernel inventory digest mismatch."));
    }
    let inventory: Value = serde_json::from_slice(&inventory).map_err(|_| invalid(UNVERIFIED))?;
    let kernel_hash = inventory["README.md"]["sha256"].as_str().unwrap_or("");
    if inventory["README.md"]["mode"] != "100644" || !hash_valid(kernel_hash, 64) {
        return Err(invalid("Published kernel inventory entry is invalid."));
    }
    let kernel = read(
        &url,
        usize::try_from(policy["maxKernelBytes"].as_u64().expect("kernel limit"))
            .map_err(|_| invalid("Kernel byte limit exceeds this platform"))?,
        "kernel",
    )?;
    if kernel.is_empty() || sha256_hex(&kernel) != kernel_hash {
        return Err(invalid("Published kernel content digest mismatch."));
    }
    let mut manifest: Value = serde_json::from_str(TEMPLATE).expect("kernel template");
    manifest["imageVersion"] = json!(format!("kernel-{release}"));
    manifest["languageVersion"] = json!(release);
    manifest["semanticSourceRevision"] = json!(commit);
    manifest["payload"][0]["byteLength"] = json!(kernel.len());
    manifest["payload"][0]["sha256"] = json!(kernel_hash);
    manifest["modelVisibleBytes"]["byteLength"] = json!(kernel.len());
    manifest["modelVisibleBytes"]["sha256"] = json!(kernel_hash);
    let mut aggregate = format!("payload/kernel.md\0{}\0", kernel.len()).into_bytes();
    aggregate.extend_from_slice(&kernel);
    aggregate.push(0);
    manifest["aggregateSha256"]["sha256"] = json!(sha256_hex(&aggregate));
    RuntimeImage::from_embedded(
        &serde_json::to_vec(&manifest).expect("manifest JSON"),
        &[
            ("payload/kernel.md", &kernel),
            ("contracts/task-envelope.schema.json", TASK),
            ("contracts/terminal.schema.json", TERMINAL),
            ("contracts/framing.txt", FRAMING),
        ],
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    const FIXTURE: &str = include_str!("../../../../shared/fixtures/kernel-startup/release.json");
    fn run(fixture: &Value) -> Result<RuntimeImage, RunnerError> {
        published_kernel_with(|url, _| {
            let r = &fixture["responses"][url];
            Ok(KernelResponse {
                status: u16::try_from(r["status"].as_u64().expect("fixture status"))
                    .expect("fixture status fits HTTP"),
                location: r["location"].as_str().map(str::to_owned),
                bytes: r["text"]
                    .as_str()
                    .expect("fixture body")
                    .as_bytes()
                    .to_vec(),
            })
        })
    }
    #[test]
    fn shared_verified_release() {
        let fixture: Value = serde_json::from_str(FIXTURE).unwrap();
        let image = run(&fixture).unwrap();
        assert_eq!(image.manifest.image_version, "kernel-fixture-1");
        assert_eq!(
            image.payload[0].bytes,
            fixture["kernel"].as_str().unwrap().as_bytes()
        );
    }
    #[test]
    fn frozen_retrieval_errors_are_staged_without_extra_requests() {
        let fixture: Value = serde_json::from_str(FIXTURE).unwrap();
        for case in fixture["retrievalFailures"].as_array().unwrap() {
            let expected_calls = case["expectedCalls"].as_array().unwrap();
            let failed_url = expected_calls.last().unwrap().as_str().unwrap();
            let mut calls = Vec::new();
            let error = published_kernel_with(|url, _| {
                calls.push(url.to_owned());
                if url == failed_url {
                    if case["trigger"] != "http" {
                        return Err(transport());
                    }
                    return Ok(KernelResponse {
                        status: u16::try_from(case["httpStatus"].as_u64().unwrap()).unwrap(),
                        location: None,
                        bytes: Vec::new(),
                    });
                }
                let response = &fixture["responses"][url];
                Ok(KernelResponse {
                    status: u16::try_from(response["status"].as_u64().unwrap()).unwrap(),
                    location: response["location"].as_str().map(str::to_owned),
                    bytes: response["text"].as_str().unwrap().as_bytes().to_vec(),
                })
            })
            .unwrap_err();
            assert_eq!(
                serde_json::to_value(error).unwrap(),
                case["error"],
                "{}",
                case["id"]
            );
            assert_eq!(json!(calls), case["expectedCalls"], "{}", case["id"]);
        }
    }
    #[test]
    fn retrieval_observation_factory_is_closed() {
        for (stage, status) in [
            ("other", None),
            ("kernel", Some(200)),
            ("kernel", Some(99)),
            ("kernel", Some(600)),
            ("entry", Some(302)),
        ] {
            assert_eq!(retrieval(stage, status).code, ErrorCode::ImageInvalid);
        }
    }
    #[test]
    fn interruption_size_and_entry_policy_failures_keep_their_classification() {
        for code in [
            ErrorCode::ImageTooLarge,
            ErrorCode::Cancelled,
            ErrorCode::StartupTimeout,
        ] {
            let original = RunnerError::catalog(code).with_detail("reason", "owned interruption");
            let error = published_kernel_with(|_, _| Err(original.clone())).unwrap_err();
            assert_eq!(error, original);
        }
        let unrelated = RunnerError::catalog(ErrorCode::HarnessFailed)
            .with_detail("reason", "private-unrelated-cause");
        let error = published_kernel_with(|_, _| Err(unrelated.clone())).unwrap_err();
        assert_eq!(
            serde_json::to_value(error).unwrap(),
            serde_json::from_str::<Value>(FIXTURE).unwrap()["retrievalFailures"][0]["error"]
        );
        for status in [100, 200, 204, 302, 399] {
            let error = published_kernel_with(|_, _| {
                Ok(KernelResponse {
                    status,
                    location: None,
                    bytes: Vec::new(),
                })
            })
            .unwrap_err();
            assert_eq!(error.code, ErrorCode::ImageInvalid);
        }
    }
    #[test]
    fn rejects_changed_origin_metadata_content_and_size() {
        let fixture: Value = serde_json::from_str(FIXTURE).unwrap();
        let entry = "https://pkg.prose.md/kernel.md";
        let root = "https://pkg.prose.md/releases/fixture-1/core/";
        for (url, field, value) in [
            (
                entry.to_owned(),
                "location",
                json!("https://example.com/releases/fixture-1/core/README.md"),
            ),
            (
                entry.to_owned(),
                "location",
                json!("/releases/../../core/README.md"),
            ),
            (entry.to_owned(), "status", json!(200)),
            (format!("{root}descriptor.json"), "text", json!("{}")),
            (format!("{root}inventory.json"), "text", json!("{}")),
            (format!("{root}README.md"), "text", json!("changed")),
            (format!("{root}README.md"), "text", json!("x".repeat(32769))),
            (format!("{root}descriptor.json"), "status", json!(503)),
        ] {
            let mut mutated = fixture.clone();
            mutated["responses"][url][field] = value.clone();
            let error = run(&mutated).unwrap_err();
            let expected = if field == "status" && value == json!(503) {
                ErrorCode::KernelRetrievalFailed
            } else if field == "text" && value.as_str().is_some_and(|text| text.len() > 32768) {
                ErrorCode::ImageTooLarge
            } else {
                ErrorCode::ImageInvalid
            };
            assert_eq!(error.code, expected);
        }
    }
}
