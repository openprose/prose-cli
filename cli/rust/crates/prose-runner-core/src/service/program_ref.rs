//! `OWNER/SLUG[@REV]` parsing and latest-to-pinned resolution. A pinned reference is `owner/slug@<16 hex rev_id>`.
use super::Context;
use super::http::{Request, encode_segment};
use crate::RunnerError;
use crate::error::ErrorCode;
use serde_json::Value;

/// A parsed program reference. `rev_number` is a numeric `@N` revision
/// (only `program show` resolves it).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgramRef {
    pub owner: String,
    pub slug: String,
    pub rev: Option<String>,
    pub rev_number: Option<u64>,
}

impl ProgramRef {
    /// `owner/slug@rev` when pinned.
    pub fn pinned(&self) -> Option<String> {
        self.rev
            .as_ref()
            .map(|rev| format!("{}/{}@{rev}", self.owner, self.slug))
    }

    /// `/p/{owner}/{slug}`.
    pub fn read_path(&self) -> String {
        format!(
            "/p/{}/{}",
            encode_segment(&self.owner),
            encode_segment(&self.slug)
        )
    }
}

/// GitHub-style handle: `^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`.
pub fn valid_owner(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=39).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
}

/// Program slug: `^[a-z0-9][a-z0-9-]{0,63}$`.
pub fn valid_slug(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

/// Revision id: 16 lowercase hex digits.
pub fn valid_rev(value: &str) -> bool {
    value.len() == 16
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// A revision number `N` of `@N` (the `rev` column), 1 to 999999999.
pub fn valid_rev_number(value: &str) -> bool {
    (1..=9).contains(&value.len())
        && value.bytes().all(|byte| byte.is_ascii_digit())
        && !value.starts_with('0')
}

/// The revision number a `@REV` names by number: `3`, or `rev3` / `r3` as
/// people write it.
fn rev_number_text(rev: &str) -> Option<&str> {
    if valid_rev_number(rev) {
        return Some(rev);
    }
    rev.strip_prefix("rev")
        .or_else(|| rev.strip_prefix('r'))
        .filter(|number| valid_rev_number(number))
}

/// `owner/slug@rev_id`, the pinned reference `program list`, `revisions` and
/// `show` print as `ref`.
pub fn ref_of(owner: &str, slug: &str, rev: &str) -> String {
    format!("{owner}/{slug}@{rev}")
}

/// A numeric `@N` where a `rev_id` is required: name the command that
/// resolves it.
fn revision_number_failure(value: &str, name: &str, rev: &str) -> RunnerError {
    RunnerError::invocation(format!(
        "program reference {value_quoted} names revision {rev} by number; this command needs the 16-hex-digit rev_id, which `cli program show {name}@{rev} --json` prints as `ref`",
        value_quoted = crate::error::quote(value)
    ))
}

/// Parses `OWNER/SLUG[@REV]`; `require_rev` rejects an unpinned reference.
pub fn parse(value: &str, require_rev: bool) -> Result<ProgramRef, RunnerError> {
    parse_with(value, require_rev, false)
}

/// [`parse`], keeping a numeric `@N` as `rev_number` when `allow_number`
/// (only `program show` resolves it); otherwise `@N` is refused with the
/// command that prints the `rev_id`.
pub fn parse_with(
    value: &str,
    require_rev: bool,
    allow_number: bool,
) -> Result<ProgramRef, RunnerError> {
    let expected = if require_rev {
        "OWNER/SLUG@REV"
    } else {
        "OWNER/SLUG[@REV]"
    };
    let invalid = || {
        RunnerError::invocation(format!(
            "program reference {value_quoted} must be {expected}: an owner handle, a lowercase slug and, after @, the 16-hex-digit rev_id",
            value_quoted = crate::error::quote(value)
        ))
    };
    let (name, rev) = match value.split_once('@') {
        Some((name, rev)) => (name, Some(rev)),
        None => (value, None),
    };
    let (owner, slug) = name.split_once('/').ok_or_else(invalid)?;
    if let Some(number) = rev.and_then(rev_number_text) {
        if valid_owner(owner) && valid_slug(slug) {
            if !allow_number {
                return Err(revision_number_failure(value, name, number));
            }
            return Ok(ProgramRef {
                owner: owner.to_owned(),
                slug: slug.to_owned(),
                rev: None,
                rev_number: number.parse().ok(),
            });
        }
    }
    if !valid_owner(owner)
        || !valid_slug(slug)
        || rev.is_some_and(|rev| !valid_rev(rev))
        || (require_rev && rev.is_none())
    {
        return Err(invalid());
    }
    Ok(ProgramRef {
        owner: owner.to_owned(),
        slug: slug.to_owned(),
        rev: rev.map(str::to_owned),
        rev_number: None,
    })
}

/// Parses `[OWNER/]SLUG[@REV]`. A bare slug is the caller's own program: the
/// returned owner is empty until [`fill_owner`] reads it, so a
/// syntax error is still reported before any request.
pub fn parse_own_allowed(value: &str) -> Result<ProgramRef, RunnerError> {
    parse_own_allowed_with(value, false)
}

/// [`parse_own_allowed`], keeping a numeric `@N` when `allow_number`.
pub fn parse_own_allowed_with(value: &str, allow_number: bool) -> Result<ProgramRef, RunnerError> {
    let (name, rev) = match value.split_once('@') {
        Some((name, rev)) => (name, Some(rev)),
        None => (value, None),
    };
    if name.contains('/') {
        return parse_with(value, false, allow_number);
    }
    if let Some(number) = rev.and_then(rev_number_text) {
        if valid_slug(name) {
            if !allow_number {
                return Err(revision_number_failure(value, name, number));
            }
            return Ok(ProgramRef {
                owner: String::new(),
                slug: name.to_owned(),
                rev: None,
                rev_number: number.parse().ok(),
            });
        }
    }
    if !valid_slug(name) || rev.is_some_and(|rev| !valid_rev(rev)) {
        return Err(RunnerError::invocation(format!(
            "program reference {value_quoted} must be [OWNER/]SLUG[@REV]: an optional owner handle, a lowercase slug and, after @, the 16-hex-digit rev_id; a bare SLUG is your own program",
            value_quoted = crate::error::quote(value)
        )));
    }
    Ok(ProgramRef {
        owner: String::new(),
        slug: name.to_owned(),
        rev: rev.map(str::to_owned),
        rev_number: None,
    })
}

/// The caller's handle and revisions from a `GET /programs/{slug}/revisions`
/// body (own programs only); `None` for an empty listing.
fn own_revisions(
    body: &serde_json::Map<String, Value>,
    slug: &str,
) -> Result<Option<(String, Vec<Value>)>, RunnerError> {
    let Some(revisions) = body.get("revisions").and_then(Value::as_array) else {
        return Err(RunnerError::catalog(ErrorCode::ServiceProtocolInvalid));
    };
    let Some(first) = revisions.first() else {
        return Ok(None);
    };
    let owner = first["owner"]
        .as_str()
        .filter(|owner| valid_owner(owner))
        .filter(|_| first["slug"] == slug)
        .ok_or_else(|| RunnerError::catalog(ErrorCode::ServiceProtocolInvalid))?;
    Ok(Some((owner.to_owned(), revisions.clone())))
}

/// `SERVICE_RESOURCE_NOT_FOUND` for a bare SLUG the caller has not saved.
fn missing_own(context: &Context<'_>, slug: &str, error: RunnerError) -> RunnerError {
    let reason = format!(
        "you have no saved program {slug_quoted}; list yours with `{}`, or pass OWNER/SLUG for another owner's public program",
        context.command("program list"),
        slug_quoted = crate::error::quote(slug)
    );
    super::not_found::explain_not_found(
        error.with_detail("reason", reason),
        context.mode,
        "program",
        slug,
        Some(&["program", "list"]),
    )
}

/// `GET /programs/{slug}/revisions` (manifest request `index`): the body, or
/// `None` on 404.
fn send_revisions(
    context: &mut Context<'_>,
    slug: &str,
    index: usize,
) -> Result<Result<serde_json::Map<String, Value>, RunnerError>, RunnerError> {
    let request = Request::from_manifest(
        context.operation,
        index,
        format!("/programs/{}/revisions", encode_segment(slug)),
    )
    .class(super::http::TransportClass::Control);
    match context.send(&request) {
        Ok(response) => Ok(Ok(response.json_object()?)),
        Err(error) if error.code == ErrorCode::ServiceResourceNotFound => Ok(Err(error)),
        Err(error) => Err(error),
    }
}

/// Fills the owner of a bare-slug reference from
/// `GET /programs/{slug}/revisions` (manifest request `index`), which lists
/// only the caller's own programs.
pub fn fill_owner(
    context: &mut Context<'_>,
    reference: &mut ProgramRef,
    index: usize,
) -> Result<(), RunnerError> {
    if !reference.owner.is_empty() {
        return Ok(());
    }
    let slug = reference.slug.clone();
    let body = match send_revisions(context, &slug, index)? {
        Ok(body) => body,
        Err(error) => return Err(missing_own(context, &slug, error)),
    };
    let Some((owner, _)) = own_revisions(&body, &slug)? else {
        return Err(missing_own(
            context,
            &slug,
            RunnerError::catalog(ErrorCode::ServiceResourceNotFound),
        ));
    };
    reference.owner = owner;
    Ok(())
}

/// Resolves a numeric `@N` of `program show` to its `rev_id` through
/// `GET /programs/{slug}/revisions` (manifest request `index`), which lists
/// only the caller's own programs; the owner is filled too. Another owner's
/// program has no revision listing, so its `@N` is refused with the `rev_id`
/// to use.
pub fn resolve_rev_number(
    context: &mut Context<'_>,
    reference: &mut ProgramRef,
    index: usize,
) -> Result<(), RunnerError> {
    let Some(number) = reference.rev_number else {
        return Ok(());
    };
    let slug = reference.slug.clone();
    let other = |context: &Context<'_>, reference: &ProgramRef| {
        let name = format!("{}/{slug}", reference.owner);
        RunnerError::invocation(format!(
            "revision numbers such as @{number} resolve only for your own programs; for {name} pass the 16-hex-digit rev_id, which `{}` prints (latest) as `ref`",
            context.command(&format!("program show {name} --json"))
        ))
    };
    let body = match send_revisions(context, &slug, index)? {
        Ok(body) => body,
        Err(_) if !reference.owner.is_empty() => return Err(other(context, reference)),
        Err(error) => return Err(missing_own(context, &slug, error)),
    };
    let Some((owner, revisions)) = own_revisions(&body, &slug)? else {
        if !reference.owner.is_empty() {
            return Err(other(context, reference));
        }
        return Err(missing_own(
            context,
            &slug,
            RunnerError::catalog(ErrorCode::ServiceResourceNotFound),
        ));
    };
    if !reference.owner.is_empty() && !reference.owner.eq_ignore_ascii_case(&owner) {
        return Err(other(context, reference));
    }
    let found = revisions
        .iter()
        .find(|item| item["rev"].as_u64() == Some(number))
        .and_then(|item| item["rev_id"].as_str())
        .filter(|rev| valid_rev(rev));
    let Some(rev) = found else {
        let newest = revisions
            .first()
            .and_then(|item| item["rev"].as_u64())
            .map_or_else(String::new, |rev| format!("; the newest is rev {rev}"));
        return Err(RunnerError::invocation(format!(
            "{owner}/{slug} has no revision {number}{newest}; list them with `{}`",
            context.command(&format!("program revisions {slug}"))
        )));
    };
    reference.rev = Some(rev.to_owned());
    reference.owner = owner;
    reference.rev_number = None;
    Ok(())
}

/// Resolves a program reference to run (`run submit --from`) to the pinned
/// `owner/slug@rev_id`, the way `program show` reads references: a bare SLUG
/// is the caller's own program, `@N` (or `@revN`) is revision N of the
/// caller's own program, a pinned `@REV` of the caller's own program must be
/// one of its revisions (a commit id is named as such, with the rev_id to
/// pass), and a latest reference reads the newest `rev_id`. Another owner's
/// pinned reference is checked by the service. `list` and `read` are the
/// operation's `GET /programs/{slug}/revisions` and `GET /p/{owner}/{slug}`
/// request indexes; `option` is the argv option the reference came from, so
/// a fix is a corrected argv.
pub fn resolve_to_run(
    context: &mut Context<'_>,
    reference: &mut ProgramRef,
    list: usize,
    read: usize,
    option: Option<&str>,
    verify_pinned: bool,
) -> Result<String, RunnerError> {
    let given = option
        .and_then(|option| context.option(option))
        .unwrap_or_default()
        .to_owned();
    if reference.owner.is_empty()
        || reference.rev_number.is_some()
        || (verify_pinned && reference.rev.is_some())
    {
        let slug = reference.slug.clone();
        let body = send_revisions(context, &slug, list)?;
        let own = match &body {
            Ok(body) => own_revisions(body, &slug)?,
            Err(_) => None,
        };
        match own {
            Some((owner, revisions))
                if reference.owner.is_empty() || reference.owner.eq_ignore_ascii_case(&owner) =>
            {
                reference.owner.clone_from(&owner);
                let name = format!("{owner}/{slug}");
                if let Some(number) = reference.rev_number.take() {
                    let found = revisions
                        .iter()
                        .find(|item| item["rev"].as_u64() == Some(number))
                        .and_then(|item| item["rev_id"].as_str())
                        .filter(|rev| valid_rev(rev));
                    let Some(rev) = found else {
                        return Err(no_revision(
                            context,
                            &name,
                            &slug,
                            &number.to_string(),
                            &revisions,
                        ));
                    };
                    reference.rev = Some(rev.to_owned());
                } else if let Some(rev) = reference.rev.clone().filter(|_| verify_pinned) {
                    if !revisions.iter().any(|item| item["rev_id"] == rev.as_str()) {
                        let commit = revisions
                            .iter()
                            .find(|item| item["commit_id"] == rev.as_str());
                        if let Some(item) = commit {
                            let rev_id = item["rev_id"].as_str().unwrap_or_default();
                            let number = item["rev"].as_u64().unwrap_or_default();
                            let fixed =
                                given.replacen(&format!("@{rev}"), &format!("@{rev_id}"), 1);
                            let error = RunnerError::invocation(format!(
                                "{rev} is the commit_id of {name} rev {number}, not a rev_id; a program reference pins the rev_id, {rev_id}"
                            ));
                            return Err(match option {
                                Some(option) => context.corrected(
                                    error,
                                    "Pass the rev_id: `{command}`",
                                    context.argv_with_option(option, &fixed),
                                ),
                                None => error,
                            });
                        }
                        return Err(no_revision(context, &name, &slug, &rev, &revisions));
                    }
                }
            }
            _ => {
                if reference.owner.is_empty() {
                    let error = body.err().unwrap_or_else(|| {
                        RunnerError::catalog(ErrorCode::ServiceResourceNotFound)
                    });
                    return Err(missing_own(context, &slug, error));
                }
                if let Some(number) = reference.rev_number {
                    let name = format!("{}/{slug}", reference.owner);
                    let mut error = RunnerError::invocation(format!(
                        "revision numbers such as @{number} resolve only for your own programs; for {name} pass the 16-hex-digit rev_id, which `{}` prints (latest) as `ref`",
                        context.command(&format!("program show {name}"))
                    ));
                    let argv = context.follow_up_argv(&["program", "show", &name]);
                    error.action = format!(
                        "Read the program's rev_id with `{}`, then pass OWNER/SLUG@REV.",
                        super::render::argv_text(&argv)
                    );
                    return Err(error.with_detail("suggestedArgv", serde_json::json!(argv)));
                }
            }
        }
    }
    let (pinned, _) = resolve(context, reference, read)?;
    Ok(pinned)
}

/// `SERVICE_RESOURCE_NOT_FOUND` for a revision the caller's program does not
/// have, naming the newest and the listing command.
fn no_revision(
    context: &Context<'_>,
    name: &str,
    slug: &str,
    rev: &str,
    revisions: &[Value],
) -> RunnerError {
    let newest = revisions
        .first()
        .and_then(|item| item["rev"].as_u64())
        .map_or_else(String::new, |rev| format!("; the newest is rev {rev}"));
    let reason = format!(
        "{name} has no revision {rev}{newest}; list them with `{}`",
        context.command(&format!("program revisions {slug}"))
    );
    super::not_found::explain_not_found(
        RunnerError::catalog(ErrorCode::ServiceResourceNotFound).with_detail("reason", reason),
        context.mode,
        "revision",
        rev,
        Some(&["program", "revisions", slug]),
    )
}

/// The `<SLUG>` of an owner-scoped verb (`program save|visibility|delete|revisions`,
/// `result publish|unpublish`): a bare slug, or `OWNER/SLUG` as `program list`
/// prints it when OWNER is the caller. Syntax is checked here,
/// before any request; [`confirm_own_slug`] checks OWNER.
pub fn parse_own_slug(value: &str) -> Result<ProgramRef, RunnerError> {
    let (name, rev) = match value.split_once('@') {
        Some((name, rev)) => (name, Some(rev)),
        None => (value, None),
    };
    let (owner, slug) = name.split_once('/').unwrap_or(("", name));
    let well_formed = (!name.contains('/') || valid_owner(owner)) && valid_slug(slug);
    if well_formed {
        if let Some(rev) = rev {
            return Err(RunnerError::invocation(format!(
                "<SLUG> names a program, not a revision; pass {slug_quoted} without @{rev}",
                slug_quoted = crate::error::quote(slug)
            )));
        }
        return Ok(ProgramRef {
            owner: owner.to_owned(),
            slug: slug.to_owned(),
            rev: None,
            rev_number: None,
        });
    }
    let lower = value.to_lowercase();
    let hint = if !value.contains('/') && lower != value && valid_slug(&lower) {
        format!(
            "; pass {lower_quoted}",
            lower_quoted = crate::error::quote(&lower)
        )
    } else {
        String::new()
    };
    Err(RunnerError::invocation(format!(
        "<SLUG> {value_quoted} must be a program slug in your own account: lowercase letters, digits and hyphens, at most 64 characters (OWNER/SLUG is accepted when OWNER is you){hint}",
        value_quoted = crate::error::quote(value)
    )))
}

/// [`parse_own_slug`] of this invocation's `<SLUG>`, with the exact fix when
/// one exists: the slug without its `@REV` (`hello@1` -> `hello`), or in
/// lowercase.
pub fn own_slug_argument(context: &Context<'_>) -> Result<ProgramRef, RunnerError> {
    let value = context.argument("SLUG").unwrap_or_default();
    parse_own_slug(value).map_err(|error| {
        let name = value.split_once('@').map_or(value, |(name, _)| name);
        let fixed = [name.to_owned(), name.to_lowercase()]
            .into_iter()
            .find(|candidate| candidate != value && parse_own_slug(candidate).is_ok());
        match fixed {
            Some(fixed) => context.corrected(
                error,
                &format!("Pass {fixed}: `{{command}}`"),
                context.argv_with_argument(value, &fixed),
            ),
            None => error,
        }
    })
}

/// Refuses OWNER when the caller's own revisions of the slug name another
/// handle. An empty listing confirms nothing; see [`unconfirmed_owner`].
pub fn check_owner(
    reference: &ProgramRef,
    body: &serde_json::Map<String, Value>,
) -> Result<(), RunnerError> {
    if reference.owner.is_empty() {
        return Ok(());
    }
    match own_revisions(body, &reference.slug)? {
        Some((owner, _)) if !owner.eq_ignore_ascii_case(&reference.owner) => {
            Err(RunnerError::invocation(format!(
                "OWNER {} is not you: your program {} is {owner}/{}, and <SLUG> names a program in your own account; pass {}",
                crate::error::quote(&reference.owner),
                crate::error::quote(&reference.slug),
                reference.slug,
                crate::error::quote(&reference.slug)
            )))
        }
        _ => Ok(()),
    }
}

/// Checks the OWNER of `OWNER/SLUG` against the caller through
/// `GET /programs/{slug}/revisions` (manifest request `index`). A bare slug
/// makes no request. When the caller has no program SLUG, OWNER cannot be
/// confirmed, so the verb is refused before anything is changed: an unconfirmed OWNER never reaches a write, and its 404 is never
/// reported as `alreadyAbsent`. A confirmed OWNER is recorded on the context
/// so the plan (`--preview`, `CONFIRMATION_REQUIRED`) says it was verified.
/// Returns the confirmed owner, or "" for a bare slug.
pub fn confirm_own_slug(
    context: &mut Context<'_>,
    reference: &ProgramRef,
    index: usize,
    creates: bool,
) -> Result<String, RunnerError> {
    if reference.owner.is_empty() {
        return Ok(String::new());
    }
    let slug = reference.slug.clone();
    let body = send_revisions(context, &slug, index)?.ok();
    let own = match &body {
        Some(body) => own_revisions(body, &slug)?,
        None => None,
    };
    let Some((owner, _)) = own else {
        if creates {
            return Err(RunnerError::invocation(format!(
                "you have no saved program {slug_quoted} yet, so OWNER {} cannot be checked against your account; a first save takes the bare slug: pass {slug_quoted}",
                crate::error::quote(&reference.owner),
                slug_quoted = crate::error::quote(&slug)
            )));
        }
        return Err(unconfirmed_owner(context, reference));
    };
    if let Some(body) = &body {
        check_owner(reference, body)?;
    }
    context.verified_owner = Some(owner.clone());
    Ok(owner)
}

/// `INVOCATION_INVALID` for an OWNER that cannot be confirmed as the caller:
/// the caller has no program SLUG to check it against.
pub fn unconfirmed_owner(context: &Context<'_>, reference: &ProgramRef) -> RunnerError {
    let slug = &reference.slug;
    let mut error = RunnerError::invocation(format!(
        "OWNER {} is not confirmed as you: you have no saved program {slug_quoted} to check it against, and <SLUG> names a program in your own account; nothing was changed. Pass the bare slug {slug_quoted} for your own program; another owner's program cannot be changed from this account",
        crate::error::quote(&reference.owner),
        slug_quoted = crate::error::quote(slug)
    ));
    error.action = format!(
        "Pass the bare slug {slug_quoted} to act on your own program; `{}` lists yours as OWNER/SLUG.",
        context.command("program list"),
        slug_quoted = crate::error::quote(slug)
    );
    error.with_detail(
        "suggestedArgv",
        serde_json::json!(context.follow_up_argv(&["program", "list"])),
    )
}

/// Resolves a reference to `owner/slug@rev_id`. A pinned reference is
/// returned without a request; otherwise `GET /p/{owner}/{slug}` (manifest
/// request `index` of the current operation) supplies the latest `rev_id`.
/// Returns the pinned reference and the program object when one was read.
pub fn resolve(
    context: &mut Context<'_>,
    reference: &ProgramRef,
    index: usize,
) -> Result<(String, Option<Value>), RunnerError> {
    if let Some(pinned) = reference.pinned() {
        return Ok((pinned, None));
    }
    let request = Request::from_manifest(context.operation, index, reference.read_path())
        .class(super::http::TransportClass::Control);
    let response = context.send(&request)?;
    let body = response.json_object()?;
    let program = body.get("program").cloned().unwrap_or(Value::Null);
    let protocol = || RunnerError::catalog(ErrorCode::ServiceProtocolInvalid);
    let rev = program["rev_id"]
        .as_str()
        .filter(|rev| valid_rev(rev))
        .ok_or_else(protocol)?;
    let owner = program["owner"]
        .as_str()
        .filter(|owner| owner.eq_ignore_ascii_case(&reference.owner))
        .ok_or_else(protocol)?;
    if program["slug"] != reference.slug.as_str() {
        return Err(protocol());
    }
    Ok((format!("{owner}/{}@{rev}", reference.slug), Some(program)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_pinned_and_latest_references() {
        let latest = parse("OpenProse/hello-world", false).unwrap();
        assert_eq!(latest.rev, None);
        assert_eq!(latest.read_path(), "/p/OpenProse/hello-world");
        let pinned = parse("openprose/hello@0123456789abcdef", true).unwrap();
        assert_eq!(pinned.pinned().unwrap(), "openprose/hello@0123456789abcdef");
        for bad in [
            "hello",
            "a/B",
            "a/b@123",
            "-a/b",
            "a/b@0123456789ABCDEF",
            "a/b/c",
        ] {
            assert!(parse(bad, false).is_err(), "{bad}");
        }
        assert!(parse("a/b", true).is_err());
    }

    #[test]
    fn revision_numbers_parse_only_where_allowed() {
        let own = parse_own_allowed_with("hello@2", true).unwrap();
        assert_eq!((own.rev_number, own.rev), (Some(2), None));
        let other = parse_with("Alice/hello@1", false, true).unwrap();
        assert_eq!((other.owner.as_str(), other.rev_number), ("Alice", Some(1)));
        for (value, allowed) in [("hello@1", false), ("a/hello@1", false), ("hello@01", true)] {
            assert!(parse_own_allowed_with(value, allowed).is_err(), "{value}");
        }
        let refused = parse("a/hello@3", true).unwrap_err();
        let reason = refused.details.unwrap()["reason"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(reason.contains("names revision 3 by number"), "{reason}");
        assert_eq!(
            ref_of("a", "hello", "0123456789abcdef"),
            "a/hello@0123456789abcdef"
        );
    }

    #[test]
    fn own_slugs_take_owner_slug_and_explain_bad_values_in_words() {
        let own = parse_own_slug("Alice/hello").unwrap();
        assert_eq!((own.owner.as_str(), own.slug.as_str()), ("Alice", "hello"));
        assert_eq!(parse_own_slug("hello").unwrap().owner, "");
        let reason = |value: &str| {
            parse_own_slug(value).unwrap_err().details.unwrap()["reason"]
                .as_str()
                .unwrap()
                .to_owned()
        };
        assert!(reason("Hello").ends_with("; pass \"hello\""));
        assert!(!reason("Hello").contains('^'));
        assert!(reason("a/hello@0123456789abcdef").contains("pass \"hello\" without @"));
        assert!(parse_own_slug("a/b/c").is_err());
    }

    #[test]
    fn bare_slugs_are_own_programs_with_the_owner_left_to_fill() {
        let own = parse_own_allowed("hello@0123456789abcdef").unwrap();
        assert_eq!((own.owner.as_str(), own.slug.as_str()), ("", "hello"));
        assert_eq!(own.rev.as_deref(), Some("0123456789abcdef"));
        assert_eq!(parse_own_allowed("Owner/hello").unwrap().owner, "Owner");
        for bad in ["Hello", "hello@123", "", "a/b/c"] {
            assert!(parse_own_allowed(bad).is_err(), "{bad}");
        }
    }
}
