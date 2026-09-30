//! The developer endpoint override, compiled only with the `dev-endpoint`
//! cargo feature. Public builds contain none of this module: they never read
//! the override variable and always talk to the production service.
//!
//! A developer sets the override variable to an https origin; the key still
//! comes from `OPENPROSE_API_KEY` or `prose cli auth login`, which stores it
//! under a credential-store entry scoped to that origin, never the
//! production one.
use super::{CREDENTIAL_ENV, Environment};
use crate::RunnerError;
use std::collections::BTreeMap;

/// The variable that overrides the service origin in a dev-endpoint build.
pub const OVERRIDE_VARIABLE: &str = "OPENPROSE_API_URL";

/// The Action of an invalid override (identical in both ports).
pub const INVALID_ACTION: &str = "Set OPENPROSE_API_URL to an https origin such as https://host.example, or unset it, then retry.";

/// The custom service named by the override variable, or `None` when it is
/// unset or empty.
///
/// # Errors
/// `CONFIG_INVALID` when the value is not an https origin.
pub fn custom(environment: &BTreeMap<String, String>) -> Result<Option<Environment>, RunnerError> {
    let Some(value) = environment
        .get(OVERRIDE_VARIABLE)
        .filter(|value| !value.is_empty())
    else {
        return Ok(None);
    };
    let origin = normalize_origin(value).map_err(|why| {
        let mut error = RunnerError::config(format!(
            "{OVERRIDE_VARIABLE} {why}; expected an https origin such as https://host.example"
        ));
        error.action = INVALID_ACTION.to_owned();
        error
    })?;
    let digest = origin_digest(&origin);
    Ok(Some(Environment {
        name: "custom",
        origin,
        credential_env: CREDENTIAL_ENV,
        store_service: format!("org.openprose.cli.custom-{digest}"),
    }))
}

/// The first 16 hex digits of the SHA-256 of an origin (identical in both
/// ports).
pub fn origin_digest(origin: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = format!("{:x}", Sha256::digest(origin.as_bytes()));
    digest[..16].to_owned()
}

/// `custom-<origin digest>`: the scope of a custom endpoint's journal
/// directory.
pub fn scope(origin: &str) -> String {
    format!("custom-{}", origin_digest(origin))
}

static INVOKED_NAME: std::sync::OnceLock<String> = std::sync::OnceLock::new();

/// The program copyable commands name, shell-quoted, when the CLI was
/// invoked under another name than `prose` (`prose-dev`, a path).
pub fn invoked_name() -> Option<&'static str> {
    INVOKED_NAME.get().map(String::as_str)
}

/// Records the program name of this process (see [`invoked_program`]).
/// The first call wins.
pub fn record_invoked_name(argv0: Option<&str>) {
    if let Some(name) = argv0.and_then(invoked_program) {
        let _ = INVOKED_NAME.set(name);
    }
}

/// The shell-quoted program that re-runs this build: `argv[0]` exactly as
/// it was invoked (a name found on `PATH` such as `prose-dev`, or a path),
/// or `None` for `prose` itself or a value that cannot be copied safely.
pub fn invoked_program(argv0: &str) -> Option<String> {
    if argv0.is_empty() || argv0 == "prose" || argv0.chars().any(|character| character.is_control())
    {
        return None;
    }
    Some(super::render::shell_quote(argv0))
}

/// The human label of a custom endpoint.
pub fn label(origin: &str) -> String {
    format!("OpenProse (custom endpoint {origin})")
}

/// `https://host[:port]` with a lowercase host, no default port and no
/// trailing slash, or why `value` is not an https origin (the same reasons
/// as the Bun port).
pub fn normalize_origin(value: &str) -> Result<String, &'static str> {
    const NOT_ORIGIN: &str = "must be an origin with no path, query or fragment";
    let Some((scheme, rest)) = value.split_once("://") else {
        return Err("is not a URL");
    };
    if !scheme.eq_ignore_ascii_case("https") {
        return Err("must use https");
    }
    let (authority, tail) = rest
        .find(['/', '?', '#'])
        .map_or((rest, ""), |at| rest.split_at(at));
    if authority.contains('@') {
        return Err("must not contain credentials");
    }
    if !tail.is_empty() && tail != "/" {
        return Err(NOT_ORIGIN);
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (authority, None),
    };
    if host.is_empty() {
        return Err("has no host");
    }
    let host = host.to_ascii_lowercase();
    let label_ok = |label: &str| {
        !label.is_empty()
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    };
    if host.len() > 253 || !host.split('.').all(label_ok) {
        return Err("is not a URL");
    }
    let port = match port {
        None => None,
        Some(port) => {
            let number = (!port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()))
                .then(|| port.parse::<u16>().ok())
                .flatten()
                .filter(|number| *number != 0)
                .ok_or("is not a URL")?;
            (number != 443).then_some(number)
        }
    };
    Ok(match port {
        Some(port) => format!("https://{host}:{port}"),
        None => format!("https://{host}"),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins_match_the_shared_vectors() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../../shared/fixtures/dev-endpoint-origins.json"
        ))
        .unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            let environment = BTreeMap::from([(
                OVERRIDE_VARIABLE.to_owned(),
                case["value"].as_str().unwrap().to_owned(),
            )]);
            match custom(&environment) {
                Ok(Some(selected)) => assert_eq!(selected.origin, case["origin"], "{}", case["id"]),
                Ok(None) => panic!("{}: no endpoint", case["id"]),
                Err(error) => assert_eq!(
                    error.details.unwrap()["reason"],
                    case["reason"],
                    "{}",
                    case["id"]
                ),
            }
        }
    }

    #[test]
    fn copyable_commands_name_the_invoked_executable() {
        assert_eq!(invoked_program("prose-dev").as_deref(), Some("prose-dev"));
        assert_eq!(
            invoked_program("./target-dev/debug/prose").as_deref(),
            Some("./target-dev/debug/prose")
        );
        assert_eq!(
            invoked_program("/opt/my tools/prose").as_deref(),
            Some("'/opt/my tools/prose'")
        );
        assert_eq!(invoked_program("prose"), None);
        assert_eq!(invoked_program(""), None);
        assert_eq!(invoked_program("prose\u{1b}[2J"), None);
        let argv = vec!["cli".to_owned(), "run".to_owned(), "list".to_owned()];
        assert_eq!(
            crate::service::render::argv_text_for("prose-dev", &argv),
            "prose-dev cli run list"
        );
    }

    fn with(value: &str) -> BTreeMap<String, String> {
        BTreeMap::from([(OVERRIDE_VARIABLE.to_owned(), value.to_owned())])
    }

    #[test]
    fn origins_are_normalized() {
        for (value, expected) in [
            ("https://example.invalid", "https://example.invalid"),
            ("https://Example.Invalid/", "https://example.invalid"),
            ("HTTPS://example.invalid:443", "https://example.invalid"),
            (
                "https://example.invalid:8443/",
                "https://example.invalid:8443",
            ),
            ("https://localhost:8787", "https://localhost:8787"),
        ] {
            assert_eq!(normalize_origin(value).as_deref(), Ok(expected), "{value}");
        }
    }

    #[test]
    fn non_origins_are_refused() {
        for value in [
            "http://example.invalid",
            "example.invalid",
            "https://",
            "https:///",
            "https://?x",
            "https://example.invalid/api",
            "https://example.invalid?x=1",
            "https://example.invalid#top",
            "https://user:pass@example.invalid",
            "https://exa mple.invalid",
            "https://example.invalid:",
            "https://example.invalid:0",
            "https://example.invalid:99999",
            "https://-bad.invalid",
            "https://example..invalid",
        ] {
            assert!(normalize_origin(value).is_err(), "{value}");
            let error = custom(&with(value)).expect_err(value);
            assert_eq!(error.code, crate::error::ErrorCode::ConfigInvalid);
            let reason = error.details.as_ref().unwrap()["reason"].as_str().unwrap();
            assert!(reason.starts_with("OPENPROSE_API_URL "), "{reason}");
            assert_eq!(error.action, INVALID_ACTION);
        }
    }

    #[test]
    fn the_override_scopes_label_store_and_journal_to_the_origin() {
        assert_eq!(custom(&BTreeMap::new()).unwrap(), None);
        assert_eq!(custom(&with("")).unwrap(), None);
        let environment = custom(&with("https://example.invalid/")).unwrap().unwrap();
        assert_eq!(environment.name, "custom");
        assert_eq!(environment.origin, "https://example.invalid");
        assert_eq!(environment.credential_env, "OPENPROSE_API_KEY");
        assert_eq!(
            environment.label(),
            "OpenProse (custom endpoint https://example.invalid)"
        );
        let digest = origin_digest("https://example.invalid");
        assert_eq!(digest.len(), 16);
        assert_eq!(
            environment.store_service,
            format!("org.openprose.cli.custom-{digest}")
        );
        assert_ne!(
            environment.store_service,
            super::super::PRODUCTION_STORE_SERVICE
        );
        assert_eq!(environment.journal_component(), format!("custom-{digest}"));
        let other = custom(&with("https://other.invalid")).unwrap().unwrap();
        assert_ne!(other.store_service, environment.store_service);
        // The store name stays inside the closed alphabet the macOS store needs.
        assert!(environment.store_service.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b".-".contains(&byte)
        }));
        // Resolution goes through the one resolver.
        assert_eq!(
            Environment::resolve(&with("https://example.invalid")).unwrap(),
            environment
        );
    }
}
