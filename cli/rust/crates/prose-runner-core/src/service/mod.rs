//! Service operations: the manifest-driven framework shared by every
//! `prose cli <noun> <verb>` that reaches the OpenProse hosted service.
//!
//! The embedded operation manifest (`shared/service/operations.v1.json`) drives
//! parsing, help, confirmation and transport classes. Feature modules
//! (`discovery`, `runs`, ...) own request bodies and result projections; this
//! module owns everything they share. Feature modules file follow-ups instead
//! of editing it.
#![allow(
    clippy::missing_errors_doc,
    clippy::missing_panics_doc,
    clippy::must_use_candidate,
    clippy::module_name_repetitions
)]

pub mod fs;
pub mod http;
pub mod journal;
pub mod not_found;
pub mod program_ref;
pub mod render;
pub mod sse;

#[cfg(feature = "dev-endpoint")]
pub mod dev_endpoint;
pub mod discovery;
pub mod jobs;
pub mod organizations;
pub mod programs;
pub mod results;
pub mod run_records;
pub mod runs;
pub mod triage;
pub mod wallet;

use crate::error::ErrorCode;
use crate::output::CommandOutcome;
use crate::{CancellationToken, GlobalFlags, OutputMode, RunnerError, SystemContext};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::sync::OnceLock;

/// The operation manifest, byte for byte (`cli service operations`).
pub const MANIFEST_TEXT: &str = include_str!("../../../../../shared/service/operations.v1.json");
/// Generated help topics (`shared/service/help.v1.json`).
pub const HELP_TEXT: &str = include_str!("../../../../../shared/service/help.v1.json");
/// The agent guide `cli service guide` prints byte for byte
/// (`shared/service/guide.v1.md`).
pub const GUIDE_TEXT: &str = include_str!("../../../../../shared/service/guide.v1.md");

/// Parsed manifest.
pub fn manifest() -> &'static Value {
    static MANIFEST: OnceLock<Value> = OnceLock::new();
    MANIFEST.get_or_init(|| serde_json::from_str(MANIFEST_TEXT).expect("embedded manifest is JSON"))
}

fn help_document() -> &'static Value {
    static HELP: OnceLock<Value> = OnceLock::new();
    HELP.get_or_init(|| serde_json::from_str(HELP_TEXT).expect("embedded help is JSON"))
}

fn help_topics() -> &'static Map<String, Value> {
    help_document()["topics"]
        .as_object()
        .expect("help topics object")
}

/// A rendered text view of the manifest (`help.v1.json` `views`).
fn help_view(name: &str) -> &'static str {
    help_document()["views"][name]
        .as_str()
        .expect("embedded help view")
}

/// The human pages of `cli service operations` (the summary table) and
/// `cli service capabilities`. Neither makes a request.
fn static_view(id: &str) -> Option<&'static str> {
    match id {
        "service.operations" => Some(help_view("cli service operations")),
        "service.capabilities" => Some(help_view("cli service capabilities")),
        _ => None,
    }
}

/// The public projection of the manifest
/// (`shared/service/operations-public.v1.json`): the allowlist of members
/// `cli service operations` prints. Service routes, service error strings, the
/// service origin, the key format and this client's own settings stay
/// internal.
pub const PUBLIC_PROJECTION_TEXT: &str =
    include_str!("../../../../../shared/service/operations-public.v1.json");

fn public_projection() -> &'static Value {
    static PROJECTION: OnceLock<Value> = OnceLock::new();
    PROJECTION.get_or_init(|| {
        serde_json::from_str(PUBLIC_PROJECTION_TEXT).expect("embedded projection is JSON")
    })
}

/// `value` reduced to the members `fields` lists: `true` copies a member
/// whole, an object applies the same allowlist to that member (to each
/// element of an array).
pub fn project_fields(value: &Value, fields: &Value) -> Value {
    match (value, fields) {
        (_, Value::Bool(true)) => value.clone(),
        (Value::Array(items), _) => Value::Array(
            items
                .iter()
                .map(|item| project_fields(item, fields))
                .collect(),
        ),
        (Value::Object(object), Value::Object(allowed)) => Value::Object(
            allowed
                .iter()
                .filter_map(|(key, sub)| {
                    object
                        .get(key)
                        .map(|item| (key.clone(), project_fields(item, sub)))
                })
                .collect(),
        ),
        _ => value.clone(),
    }
}

/// The published manifest: the embedded one reduced to its public
/// projection.
pub fn public_manifest() -> Value {
    project_fields(manifest(), &public_projection()["fields"])
}

/// The result of `cli service operations` (the published manifest) or `cli
/// service capabilities` (the capabilities document) in JSON modes: the
/// envelope's `result`, like every other command's.
fn static_result(id: &str) -> Option<Value> {
    match id {
        "service.operations" => Some(public_manifest()),
        "service.capabilities" => Some(help_document()["capabilities"].clone()),
        _ => None,
    }
}

/// A guide section id: the title lowercased, apostrophes dropped, every other
/// run of characters outside `[a-z0-9]` one hyphen, no leading or trailing
/// hyphen (`ci/render_service_help.py` `guide_slug`).
fn guide_slug(title: &str) -> String {
    let mut slug = String::new();
    let mut pending = false;
    for character in title.to_lowercase().chars().filter(|c| *c != '\'') {
        if character.is_ascii_lowercase() || character.is_ascii_digit() {
            if pending && !slug.is_empty() {
                slug.push('-');
            }
            pending = false;
            slug.push(character);
        } else {
            pending = true;
        }
    }
    slug
}

/// `cli service guide --json`: `{sections: [{id, title, body}]}`.
/// A section starts at each line beginning `## `; its body is every following
/// line up to the next section, without leading or trailing LF. The text
/// before the first section (the `# ` title) is not a section. This is the
/// split of `ci/render_service_help.py` `guide_sections`, which generates the
/// corpus cases pinning both ports.
fn guide_result() -> Value {
    let mut sections: Vec<(String, Vec<&str>)> = Vec::new();
    for line in GUIDE_TEXT.split('\n') {
        if let Some(title) = line.strip_prefix("## ") {
            sections.push((title.to_owned(), Vec::new()));
        } else if let Some((_, lines)) = sections.last_mut() {
            lines.push(line);
        }
    }
    let sections = sections
        .into_iter()
        .map(|(title, lines)| {
            json!({
                "id": guide_slug(&title),
                "title": title,
                "body": lines.join("\n").trim_matches('\n'),
            })
        })
        .collect::<Vec<_>>();
    json!({ "sections": sections })
}

/// Every manifest operation.
pub fn operations() -> &'static [Value] {
    manifest()["operations"]
        .as_array()
        .map_or(&[], Vec::as_slice)
}

/// Looks up an operation by id.
pub fn operation(id: &str) -> Option<&'static Value> {
    operations().iter().find(|operation| operation["id"] == id)
}

/// An operation's command path: its manifest `command` argv without the
/// leading `cli` (`["run", "submit"]`).
fn command_words(operation: &Value) -> Vec<&str> {
    operation["command"]
        .as_array()
        .map(|words| {
            words
                .iter()
                .filter_map(Value::as_str)
                .skip_while(|word| *word == "cli")
                .collect()
        })
        .unwrap_or_default()
}

/// The words after `cli` that repeat this invocation for the next page
///: the command and every option given, in manifest order,
/// except `--before`.
fn page_words(operation: &Value, invocation: &ServiceInvocation) -> Vec<String> {
    let mut words = command_words(operation)
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    for option in operation["options"]
        .as_array()
        .map_or(&[][..], Vec::as_slice)
    {
        let Some(name) = option["name"].as_str().filter(|name| *name != "--before") else {
            continue;
        };
        for value in invocation.option_values(name) {
            words.extend([name.to_owned(), value.clone()]);
        }
    }
    words
}

fn is_service_operation(operation: &Value) -> bool {
    operation["contract"] == "service/1"
}

/// Nouns owned by service operations (`service`, `run`, ...).
fn service_nouns() -> BTreeSet<&'static str> {
    operations()
        .iter()
        .filter(|operation| is_service_operation(operation))
        .filter_map(|operation| command_words(operation).first().copied())
        .collect()
}

/// A parsed service invocation after `cli`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServiceCommand {
    /// Exact help text for a manifest topic.
    Help(String),
    Invoke(Box<ServiceInvocation>),
    /// A service-noun invocation that names no operation (unknown or missing
    /// verb, misspelled noun, global option after `cli`). Rendered by the
    /// service renderer, never the runner one.
    Invalid(Box<ServiceInvalid>),
}

/// How to fix a rejected service invocation. `action` is the
/// per-cause Action; `{command}` in it is replaced by the rendered command
/// line, which is also returned as `details.suggestedArgv`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Correction {
    /// A corrected copy of the original argv (after the product name).
    Argv { action: String, argv: Vec<String> },
    /// A fresh command (words after `cli`) in the invocation's output mode, rendered by [`render::follow_up_argv`].
    Command { action: String, words: Vec<String> },
    /// No command applies.
    Text(String),
}

/// A service-noun invocation rejected before an operation was known.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ServiceInvalid {
    pub error: Option<RunnerError>,
    pub correction: Option<Correction>,
    /// `--output` found anywhere after `cli`.
    pub output: Option<OutputMode>,
    pub json: bool,
    /// The complete original argv (after the executable), set by the entry
    /// point: in JSON modes the envelope's `operation` is the one it names
    /// ([`command_operation`]).
    pub argv: Vec<String>,
}

/// One manifest operation with its parsed argv.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ServiceInvocation {
    pub operation: String,
    /// Positional arguments by manifest name (a variadic argument keeps all values).
    pub arguments: BTreeMap<String, Vec<String>>,
    /// Value options by manifest name; flags carry no values.
    pub options: BTreeMap<String, Vec<String>>,
    pub flags: BTreeSet<String>,
    pub json: bool,
    pub yes: bool,
    pub preview: bool,
    /// `--output` given after the command path.
    pub output: Option<OutputMode>,
    /// The complete original argv (after the executable), for copyable retries.
    pub argv: Vec<String>,
    /// A parse failure attributed to this known operation.
    pub error: Option<RunnerError>,
    /// How to fix `error`.
    pub correction: Option<Correction>,
}

impl ServiceInvocation {
    pub fn argument(&self, name: &str) -> Option<&str> {
        self.arguments
            .get(name)
            .and_then(|values| values.first())
            .map(String::as_str)
    }

    pub fn option(&self, name: &str) -> Option<&str> {
        self.options
            .get(name)
            .and_then(|values| values.first())
            .map(String::as_str)
    }

    pub fn option_values(&self, name: &str) -> &[String] {
        self.options.get(name).map_or(&[], Vec::as_slice)
    }

    pub fn flag(&self, name: &str) -> bool {
        self.flags.contains(name)
    }
}

/// Whether `args` (the tokens after `cli`) belong to the service parser. The
/// frozen account paths (`auth`, `package`, `org list`) and the
/// runner operations keep their own parsers.
pub fn claims(args: &[String]) -> bool {
    let Some(noun) = args.first() else {
        return false;
    };
    if noun == "org" && args.get(1).is_some_and(|verb| verb == "list") {
        return false;
    }
    if noun == "org" && args.len() == 2 && args[1] == "--help" {
        return true;
    }
    service_nouns().contains(noun.as_str())
}

/// Whether `noun verb` is a frozen account operation (`auth status`,
/// `org list`, `package fetch`, ...).
fn is_account_verb(noun: &str, verb: &str) -> bool {
    operations().iter().any(|operation| {
        operation["contract"] == "account/1" && command_words(operation) == [noun, verb]
    })
}

/// The help for `cli --help`, `cli <noun> --help` of every manifest noun
/// (including the frozen account groups `auth` and `package`)
/// and `cli <noun> <verb> --help` of a frozen service operation. Every topic
/// comes from `help.v1.json`, so none of them prints the runner help
///. Runner operations (`doctor`, `harness`, ...) keep it.
pub fn group_help(args: &[String]) -> Option<String> {
    let values = args.iter().map(String::as_str).collect::<Vec<_>>();
    let topic = match values.as_slice() {
        ["--help"] => "cli".to_owned(),
        [noun, "--help"] if help_topics().contains_key(&format!("cli {noun}")) => {
            format!("cli {noun}")
        }
        // Account operations keep their own parser, which never takes a value
        // starting with `-`, so `--help` or `-h` anywhere after a known verb
        // asks for that verb's topic.
        [noun, verb, rest @ ..]
            if (rest.contains(&"--help") || rest.contains(&"-h"))
                && is_account_verb(noun, verb) =>
        {
            format!("cli {noun} {verb}")
        }
        // A nested command group such as `org member` or `job contract`.
        [noun, words @ .., "--help"]
            if !words.is_empty()
                && service_nouns().contains(noun)
                && !verbs_after(&values[..values.len() - 1]).is_empty() =>
        {
            format!("cli {}", values[..values.len() - 1].join(" "))
        }
        _ => return None,
    };
    help_topics()
        .get(&topic)
        .and_then(Value::as_str)
        .map(str::to_owned)
}

/// A service help request's output: the text, or in a JSON mode the
/// envelope whose result is `{help, operations}` (the text and the manifest
/// records of the commands it describes), so a suggested help command still
/// prints one parseable line. `operation` is the one command a topic names,
/// else `service.operations`.
#[must_use]
pub fn help_outcome(text: &str, mode: OutputMode) -> CommandOutcome {
    let shown = render::localize_help(text);
    if mode == OutputMode::Human {
        return CommandOutcome::human(shown, "", 0);
    }
    let topic = help_topics()
        .iter()
        .find(|(_, value)| value.as_str() == Some(text))
        .map_or("cli", |(topic, _)| topic.as_str());
    let words = topic.split(' ').skip(1).collect::<Vec<_>>();
    // The records as `cli service operations` publishes them.
    let published = public_manifest();
    let records = operations()
        .iter()
        .zip(published["operations"].as_array().into_iter().flatten())
        .filter(|(operation, _)| command_words(operation).starts_with(&words))
        .map(|(_, record)| record.clone())
        .collect::<Vec<_>>();
    let named = operations()
        .iter()
        .find(|operation| command_words(operation) == words)
        .or_else(|| operation("service.operations"))
        .expect("the manifest has service.operations");
    let document = json!({
        "schema": "openprose.service-operation/1",
        "operation": named["id"],
        "interaction": named["interaction"],
        "result": {"help": shown, "operations": records},
        "problem": null,
    });
    if mode == OutputMode::Jsonl {
        CommandOutcome::jsonl(vec![document], 0)
    } else {
        CommandOutcome::json(document, 0)
    }
}

/// The parser intent-inference tables (`grammar.intentInference`).
fn inference() -> &'static Value {
    &manifest()["grammar"]["intentInference"]
}

/// Runner commands outside the manifest, for the unknown-command listing and
/// verb suggestions.
const RUNNER_COMMANDS: &[&[&str]] = &[
    &["doctor"],
    &["harness", "list"],
    &["harness", "use"],
    &["cleanup", "prime"],
    &["config", "explain"],
];

/// Every command path after `cli`: manifest operations (account and service)
/// and the runner commands.
fn command_paths() -> Vec<Vec<&'static str>> {
    let mut paths = operations().iter().map(command_words).collect::<Vec<_>>();
    paths.extend(RUNNER_COMMANDS.iter().map(|path| path.to_vec()));
    paths
}

/// Every first word after `cli`, sorted: the unknown-command listing.
fn all_nouns() -> Vec<&'static str> {
    command_paths()
        .into_iter()
        .filter_map(|path| path.first().copied())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// Whether `words` is a command group or a complete command path.
fn is_command_prefix(words: &[&str]) -> bool {
    !words.is_empty()
        && command_paths()
            .iter()
            .any(|path| path.len() >= words.len() && path[..words.len()] == *words)
}

/// Optimal-string-alignment distance: Levenshtein plus adjacent
/// transpositions counted as one edit (`lsit` -> `list` is 1).
fn distance(left: &str, right: &str) -> usize {
    let a = left.chars().collect::<Vec<_>>();
    let b = right.chars().collect::<Vec<_>>();
    let mut table = vec![vec![0usize; b.len() + 1]; a.len() + 1];
    for (i, row) in table.iter_mut().enumerate() {
        row[0] = i;
    }
    for j in 0..=b.len() {
        table[0][j] = j;
    }
    for i in 1..=a.len() {
        for j in 1..=b.len() {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            let mut best = (table[i - 1][j] + 1)
                .min(table[i][j - 1] + 1)
                .min(table[i - 1][j - 1] + cost);
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                best = best.min(table[i - 2][j - 2] + 1);
            }
            table[i][j] = best;
        }
    }
    table[a.len()][b.len()]
}

/// The largest distance a suggestion may have for `word`.
fn max_distance(word: &str) -> usize {
    let limits = &inference()["distance"];
    let number = |key: &str| {
        limits[key]
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .unwrap_or(1)
    };
    if word.chars().count() <= number("shortWordLength") {
        number("shortWordMax")
    } else {
        number("max")
    }
}

/// The unique nearest candidate within the manifest distance limit, if any.
pub fn did_you_mean<'a>(
    word: &str,
    candidates: impl IntoIterator<Item = &'a str>,
) -> Option<&'a str> {
    did_you_mean_within(word, candidates, max_distance(word))
}

/// [`did_you_mean`] with an explicit distance limit.
fn did_you_mean_within<'a>(
    word: &str,
    candidates: impl IntoIterator<Item = &'a str>,
    limit: usize,
) -> Option<&'a str> {
    let mut found = candidates
        .into_iter()
        .filter(|candidate| *candidate != word)
        .map(|candidate| (distance(word, candidate), candidate))
        .filter(|(distance, _)| *distance <= limit)
        .collect::<Vec<_>>();
    found.sort_unstable();
    found.dedup();
    match found.as_slice() {
        [(best, candidate), rest @ ..] if rest.first().is_none_or(|(next, _)| next > best) => {
            Some(*candidate)
        }
        _ => None,
    }
}

/// Up to `count` candidates nearest to `word` (edit distance, then name),
/// for listing alternatives when no unique suggestion exists.
pub fn nearest<'a>(word: &str, candidates: &[&'a str], count: usize) -> Vec<&'a str> {
    let mut ranked = candidates
        .iter()
        .map(|candidate| (distance(word, candidate), *candidate))
        .collect::<Vec<_>>();
    ranked.sort_unstable();
    ranked.dedup();
    ranked
        .into_iter()
        .take(count)
        .map(|(_, candidate)| candidate)
        .collect()
}

/// The first candidate of a synonym-table entry that `valid` accepts.
fn synonym(table: &str, word: &str, valid: impl Fn(&str) -> bool) -> Option<&'static str> {
    inference()[table][word]
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .find(|candidate| valid(candidate))
}

/// A verb for an unknown word in a group: a synonym first, then the nearest.
fn suggest_verb(word: &str, verbs: &[&'static str]) -> Option<&'static str> {
    synonym("verbSynonyms", word, |candidate| verbs.contains(&candidate))
        .or_else(|| did_you_mean(word, verbs.iter().copied()))
}

/// The command words for an unknown word right after `cli`: a noun synonym
/// (`organization` -> `org`, `whoami` -> `auth status`) first, then the
/// nearest noun.
fn suggest_noun_words(noun: &str) -> Option<Vec<&'static str>> {
    let listed = inference()["nounSynonyms"][noun]
        .as_array()
        .map(|words| words.iter().filter_map(Value::as_str).collect::<Vec<_>>())
        .filter(|words| is_command_prefix(words));
    listed.or_else(|| did_you_mean(noun, all_nouns()).map(|found| vec![found]))
}

/// The `optionAliases` target of an unknown option: the first candidate
/// that is one of `names` and `fits` the given value.
fn option_alias<'a>(name: &str, names: &[&'a str], fits: impl Fn(&str) -> bool) -> Option<&'a str> {
    inference()["optionAliases"][name]
        .as_array()
        .and_then(|candidates| {
            candidates
                .iter()
                .filter_map(Value::as_str)
                .filter(|candidate| fits(candidate))
                .find_map(|candidate| names.iter().copied().find(|known| *known == candidate))
        })
}

/// The largest distance an option suggestion may have: a name of at most
/// `optionShortWordLength` characters without its leading dashes allows
/// `optionShortWordMax` (`--cron` is never `--json`); a longer one the
/// ordinary limit.
fn option_distance_limit(name: &str) -> usize {
    let limits = &inference()["distance"];
    let ordinary = max_distance(name);
    let bare = name.trim_start_matches('-').chars().count();
    match (
        limits["optionShortWordLength"].as_u64(),
        limits["optionShortWordMax"].as_u64(),
    ) {
        (Some(length), Some(max)) if u64::try_from(bare).is_ok_and(|bare| bare <= length) => {
            ordinary.min(usize::try_from(max).unwrap_or(ordinary))
        }
        _ => ordinary,
    }
}

/// A canonical option for an unknown one: an alias first, then the nearest
/// within the option distance limit, which counts only when its value type
/// fits: a flag never takes the value the unknown option was given
/// (`has_value`).
fn suggest_option<'a>(
    name: &str,
    names: &[&'a str],
    fits: impl Fn(&str) -> bool,
    has_value: bool,
    takes_value: impl Fn(&str) -> bool,
) -> Option<&'a str> {
    option_alias(name, names, fits).or_else(|| {
        did_you_mean_within(name, names.iter().copied(), option_distance_limit(name))
            .filter(|near| !has_value || takes_value(near))
    })
}

/// A unit-changing spelling of an option (`optionConversions`): `(target option, multiplier, from unit, to unit)`, when the target
/// is one of `names`.
fn option_conversion<'a>(
    name: &str,
    names: &[&'a str],
) -> Option<(&'a str, u64, &'static str, &'static str)> {
    let entry = &inference()["optionConversions"][name];
    let target = entry["option"].as_str()?;
    let target = names.iter().copied().find(|known| *known == target)?;
    Some((
        target,
        entry["multiplier"].as_u64()?,
        entry["from"].as_str()?,
        entry["to"].as_str()?,
    ))
}

/// `value` times `multiplier` when `value` is a positive whole number (at
/// most 9 digits); `None` otherwise, so nothing else is ever converted.
fn converted_value(value: &str, multiplier: u64) -> Option<String> {
    if value.is_empty() || value.len() > 9 || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let number = value.parse::<u64>().ok().filter(|number| *number > 0)?;
    number
        .checked_mul(multiplier)
        .map(|product| product.to_string())
}

/// The placeholder of a secret-reading option (`secretFileOptions`), such as `CODE` for `--code-file`.
fn secret_placeholder(name: &str) -> Option<&'static str> {
    inference()["secretFileOptions"][name].as_str()
}

/// A command phrase that is not a command path (`commandRewrites`).
#[derive(Clone)]
struct Rewrite {
    command: Vec<&'static str>,
    append: Vec<&'static str>,
    /// The option the typed argument becomes (`--from` for `program run SLUG`).
    argument_option: Option<&'static str>,
    /// The command a typed positional argument selects instead (`why RUN_ID`
    /// is `run show RUN_ID`).
    argument_command: Option<Vec<&'static str>>,
    /// A sentence the Action starts with.
    note: Option<&'static str>,
    why: &'static str,
}

fn command_rewrite(phrase: &str) -> Option<Rewrite> {
    let entry = &inference()["commandRewrites"][phrase];
    let words = entry["command"]
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>();
    let append = entry["append"]
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>();
    Some(Rewrite {
        command: words,
        append,
        argument_option: entry["argumentOption"].as_str(),
        argument_command: entry["argumentCommand"]
            .as_array()
            .map(|words| words.iter().filter_map(Value::as_str).collect()),
        note: entry["note"].as_str(),
        why: entry["why"].as_str()?,
    })
}

/// An exact command spelling that runs another command (`commandAliases`,
/// `cli run status` -> `cli run show`): the target operation and how many
/// typed words the alias spans. Aliases are hidden from help and listings.
fn command_alias(tokens: &[&str]) -> Option<(&'static Value, usize)> {
    let aliases = inference()["commandAliases"].as_object()?;
    aliases.iter().find_map(|(phrase, target)| {
        let words = phrase.split(' ').collect::<Vec<_>>();
        if tokens.len() < words.len() || tokens[..words.len()] != words[..] {
            return None;
        }
        let target = target
            .as_array()?
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>();
        operations()
            .iter()
            .filter(|operation| is_service_operation(operation))
            .find(|operation| command_words(operation) == target)
            .map(|operation| (operation, words.len()))
    })
}

/// An option dropped with a reason (`optionRemovals`) for operation `id`:
/// `(why, whether it takes a value)`.
fn option_removal(name: &str, id: &str) -> Option<(&'static str, bool)> {
    let entry = &inference()["optionRemovals"][name];
    let operations = entry["operations"].as_array()?;
    if !operations.is_empty() && !operations.iter().any(|operation| operation == id) {
        return None;
    }
    Some((entry["why"].as_str()?, entry["value"] == true))
}

/// A whole command line typed as one word (quoted, so the shell did not
/// split it): its words, when the first names `cli` or a command group.
fn unsplit_words(token: &str) -> Option<Vec<&str>> {
    if !token.contains(char::is_whitespace) {
        return None;
    }
    let words = token.split_whitespace().collect::<Vec<_>>();
    let first = *words.first()?;
    (first == "cli" || is_command_prefix(&[first])).then_some(words)
}

/// Whether `words` is a complete command path (an operation or a runner command).
fn is_command_path(words: &[&str]) -> bool {
    command_paths().iter().any(|path| path[..] == *words)
}

/// Completes a corrected command prefix against the tokens that follow it
///: a group gains the next typed verb, the verb a misspelled
/// next word means, or its only verb. Returns the full path and how many
/// following tokens it replaces, or `None` when the group needs a verb the
/// argv does not give and has several (the caller names its help).
fn complete_path<'a>(mut path: Vec<&'a str>, rest: &[&str]) -> Option<(Vec<&'a str>, usize)> {
    let mut consumed = 0;
    while !is_command_path(&path) {
        let verbs = verbs_after(&path);
        let next = rest
            .get(consumed)
            .copied()
            .filter(|word| !word.starts_with('-'));
        let verb = match next {
            Some(word) => verbs
                .iter()
                .copied()
                .find(|verb| *verb == word)
                .or_else(|| suggest_verb(word, &verbs)),
            None => None,
        };
        match (verb, verbs.as_slice()) {
            (Some(verb), _) => {
                path.push(verb);
                consumed += 1;
            }
            (None, [only]) if next.is_none() => path.push(only),
            _ => return None,
        }
    }
    Some((path, consumed))
}

/// When the tokens after `cli` ask for help without `--help` (bare `cli`,
/// `cli help [COMMAND...]`, a trailing `help` or `-h` after a command group or
/// path), the equivalent tokens ending in `--help`.
pub fn help_request(args: &[String]) -> Option<Vec<String>> {
    let words = args.iter().map(String::as_str).collect::<Vec<_>>();
    let with_help = |words: &[&str]| {
        words
            .iter()
            .map(|word| (*word).to_owned())
            .chain(std::iter::once("--help".to_owned()))
            .collect::<Vec<_>>()
    };
    match words.as_slice() {
        [] => Some(with_help(&[])),
        ["help", rest @ ..] if rest.iter().all(|word| !word.starts_with('-')) => {
            Some(with_help(rest))
        }
        [prefix @ .., "help" | "-h"] if is_command_prefix(prefix) => Some(with_help(prefix)),
        _ => None,
    }
}

fn verbs_after(prefix: &[&str]) -> Vec<&'static str> {
    let mut verbs = command_paths()
        .into_iter()
        .filter(|words| words.len() > prefix.len() && words[..prefix.len()] == *prefix)
        .map(|words| words[prefix.len()])
        .collect::<Vec<_>>();
    verbs.sort_unstable();
    verbs.dedup();
    verbs
}

fn invalid(reason: impl Into<String>) -> RunnerError {
    RunnerError::invocation(reason)
}

/// A rejected invocation and how to fix it.
struct Rejection {
    error: RunnerError,
    correction: Correction,
}

fn reject(reason: impl Into<String>, correction: Correction) -> Rejection {
    Rejection {
        error: invalid(reason),
        correction,
    }
}

/// Where the tokens being parsed sit inside the original argv (they are a
/// suffix of it), so a correction can be a copy of the original command.
struct Positions<'a> {
    original: &'a [String],
    offset: Option<usize>,
}

impl<'a> Positions<'a> {
    fn new(original: &'a [String], tokens: &[String]) -> Self {
        let offset = original
            .len()
            .checked_sub(tokens.len())
            .filter(|offset| original[*offset..] == *tokens);
        Self { original, offset }
    }

    /// The original argv with `len` tokens at `at` replaced by `with`.
    fn splice(&self, at: usize, len: usize, with: &[&str]) -> Option<Vec<String>> {
        let start = self.offset? + at;
        let end = start
            .checked_add(len)
            .filter(|end| *end <= self.original.len())?;
        let mut argv = self.original[..start].to_vec();
        argv.extend(with.iter().map(|word| (*word).to_owned()));
        argv.extend_from_slice(&self.original[end..]);
        Some(argv)
    }
}

/// The slug a program file suggests (`hello.prose.md` -> `hello`), when
/// it is a valid slug (identical in both ports).
fn slug_from_file(file: &str) -> Option<String> {
    let base = file.rsplit('/').next()?;
    let stem = base
        .strip_suffix(".prose.md")
        .or_else(|| base.strip_suffix(".md"))?;
    let valid = (1..=64).contains(&stem.len())
        && stem
            .bytes()
            .next()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && stem
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');
    valid.then(|| stem.to_owned())
}

/// `prose ... cli <command> --help`, the fallback when no exact fix exists.
fn help_words(command: &[&str]) -> Vec<String> {
    command
        .iter()
        .map(|word| (*word).to_owned())
        .chain(std::iter::once("--help".to_owned()))
        .collect()
}

/// A corrected copy of the argv, or the command's help when the tokens could
/// not be located.
fn argv_or_help(
    action: String,
    argv: Option<Vec<String>>,
    command: &[&str],
    fallback: &str,
) -> Correction {
    match argv {
        Some(argv) => Correction::Argv { action, argv },
        None => Correction::Command {
            action: fallback.to_owned(),
            words: help_words(command),
        },
    }
}

/// The listing that shows valid values for a missing positional argument.
fn lister(
    command: &[&str],
    argument: &str,
    invocation: &ServiceInvocation,
) -> Option<(&'static str, Vec<String>)> {
    let words = |values: &[&str]| {
        values
            .iter()
            .map(|word| (*word).to_owned())
            .collect::<Vec<_>>()
    };
    match argument {
        "RUN_ID" => Some(("runs", words(&["run", "list"]))),
        "JOB_ID" => Some(("jobs", words(&["job", "list"]))),
        "ORG" => Some(("organizations", words(&["org", "list"]))),
        "NAME" if command.first() == Some(&"example") => {
            Some(("examples", words(&["example", "list"])))
        }
        "SLUG" if matches!(command.first(), Some(&("program" | "result"))) => {
            Some(("your programs", words(&["program", "list"])))
        }
        "OWNER/SLUG" | "OWNER/SLUG[@REV]" | "OWNER/SLUG@REV" => {
            Some(("programs", words(&["program", "list"])))
        }
        "PUBLICATION_ID" => invocation
            .argument("OWNER/SLUG")
            .map(|program| ("publications", words(&["result", "list", program]))),
        "ACCOUNT_ID" => invocation
            .argument("ORG")
            .map(|org| ("members", words(&["org", "member", "list", org]))),
        _ => None,
    }
}

/// Parses the tokens after `cli` for a service operation. `argv` is the
/// complete original argument list, kept for copyable retries.
pub fn parse(args: &[String], original: &[String]) -> Result<ServiceCommand, RunnerError> {
    parse_with(args, original, true)
}

/// [`parse`] without completing its corrections (the second parse of
/// [`settle`]).
fn parse_unsettled(args: &[String], original: &[String]) -> Result<ServiceCommand, RunnerError> {
    parse_with(args, original, false)
}

fn parse_with(
    args: &[String],
    original: &[String],
    settled: bool,
) -> Result<ServiceCommand, RunnerError> {
    if let Some(help) = group_help(args) {
        return Ok(ServiceCommand::Help(help));
    }
    let tokens = args.iter().map(String::as_str).collect::<Vec<_>>();
    // Longest command path that names an operation.
    let mut best: Option<&'static Value> = None;
    for operation in operations()
        .iter()
        .filter(|operation| is_service_operation(operation))
    {
        let words = command_words(operation);
        if tokens.len() >= words.len()
            && tokens[..words.len()] == words[..]
            && best.is_none_or(|current| command_words(current).len() < words.len())
        {
            best = Some(operation);
        }
    }
    // An exact alias spelling (`cli run status RUN_ID`) runs its target.
    let alias = command_alias(&tokens)
        .filter(|(_, len)| best.is_none_or(|current| command_words(current).len() < *len));
    let (operation, typed) = match (alias, best) {
        (Some((operation, len)), _) => (operation, len),
        (None, Some(operation)) => (operation, command_words(operation).len()),
        (None, None) => {
            let positions = Positions::new(original, args);
            let rejection = if settled {
                unknown_command(&tokens, &positions)
            } else {
                unknown_command_unsettled(&tokens, &positions)
            };
            return Ok(ServiceCommand::Invalid(Box::new(invalid_command(
                rejection, args,
            ))));
        }
    };
    let words = command_words(operation);
    let rest = &args[typed..];
    if rest.iter().any(|token| token == "--help" || token == "-h") && help_position(operation, rest)
    {
        let topic = format!("cli {}", words.join(" "));
        return help_topics()
            .get(&topic)
            .and_then(Value::as_str)
            .map(|text| ServiceCommand::Help(text.to_owned()))
            .ok_or_else(|| invalid(format!("no help topic for `{topic}`")));
    }
    let mut invocation = ServiceInvocation {
        operation: operation["id"].as_str().unwrap_or_default().to_owned(),
        argv: original.to_vec(),
        ..ServiceInvocation::default()
    };
    let positions = Positions::new(original, rest);
    if let Err(rejection) = parse_operation_arguments(operation, rest, &mut invocation, &positions)
    {
        let rejection = if settled {
            settle(rejection)
        } else {
            rejection
        };
        invocation.error = Some(rejection.error);
        invocation.correction = Some(rejection.correction);
        // Early global-option scan: a trailing `--output` after the rejected
        // token still selects the output mode of every suggested command.
        let scanned = scan_globals(rest);
        if invocation.output.is_none() {
            invocation.output = scanned.output;
        }
        invocation.json |= scanned.json;
    }
    Ok(ServiceCommand::Invoke(Box::new(invocation)))
}

/// A service-noun invocation that names no operation.
fn invalid_command(rejection: Rejection, args: &[String]) -> ServiceInvalid {
    let scanned = scan_globals(args);
    ServiceInvalid {
        error: Some(rejection.error),
        correction: Some(rejection.correction),
        ..scanned
    }
}

/// Tolerant scan of the tokens after `cli` for the options that choose the
/// output mode (valid values only, first occurrence wins,
/// nothing after `--`). Used only to render an error that stopped parsing.
fn scan_globals(tokens: &[String]) -> ServiceInvalid {
    let mut found = ServiceInvalid::default();
    let mut index = 0;
    while let Some(token) = tokens.get(index) {
        index += 1;
        if token == "--" {
            break;
        }
        let (name, value) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_owned())),
            _ => (token.as_str(), None),
        };
        // A rejected spelling of --output (`--format json`, `-o json`) still
        // chooses how the error is printed.
        let output_alias = inference()["optionAliases"][name]
            .as_array()
            .is_some_and(|targets| targets.iter().any(|target| target == "--output"));
        match name {
            "--json" if value.is_none() => found.json = true,
            _ if name == "--output" || output_alias => {
                let value = match value {
                    Some(value) => value,
                    None => match tokens.get(index) {
                        Some(value) => {
                            index += 1;
                            value.clone()
                        }
                        None => break,
                    },
                };
                if found.output.is_none() {
                    found.output = OutputMode::parse(&value).ok();
                }
            }
            _ => {}
        }
    }
    found
}

/// Handles a service noun whose first token after `cli` is a runner-global
/// option (`cli --output json run list`): the option belongs
/// before `cli`. Returns `None` when the tokens are not a service invocation.
pub fn misplaced_globals(tokens: &[String], original: &[String]) -> Option<ServiceCommand> {
    let mut index = 0;
    let mut moved: Vec<String> = Vec::new();
    let mut names: Vec<String> = Vec::new();
    while let Some(token) = tokens.get(index) {
        let name = token
            .split_once('=')
            .map_or(token.as_str(), |(name, _)| name);
        match name {
            "--output" => {
                let width = if token.contains('=') { 1 } else { 2 };
                let span = tokens.get(index..index + width)?;
                moved.extend_from_slice(span);
                names.push(name.to_owned());
                index += width;
            }
            "--no-color" | "--verbose" if !token.contains('=') => {
                moved.push(token.clone());
                names.push(name.to_owned());
                index += 1;
            }
            _ => break,
        }
    }
    if names.is_empty() || !claims(&tokens[index..]) {
        return None;
    }
    let positions = Positions::new(original, tokens);
    let argv = positions.offset.and_then(|offset| {
        let cli = offset
            .checked_sub(1)
            .filter(|cli| original[*cli] == "cli")?;
        let mut argv = original[..cli].to_vec();
        argv.extend(moved.iter().cloned());
        argv.push("cli".to_owned());
        argv.extend_from_slice(&tokens[index..]);
        Some(argv)
    });
    let listed = names
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>()
        .join(", ");
    let rejection = reject(
        format!("the global option {listed} must come before `cli`, not after it"),
        match argv {
            Some(argv) => Correction::Argv {
                action: "Place global options before cli: `{command}`".to_owned(),
                argv,
            },
            None => Correction::Text("Place global options before cli and retry.".to_owned()),
        },
    );
    Some(ServiceCommand::Invalid(Box::new(invalid_command(
        rejection, tokens,
    ))))
}

/// Any tokens after `cli` that no parser claimed: an unknown or misspelled
/// command, or a runner or service group with an unknown verb. Rendered by the service renderer so both ports
/// print the same bytes.
pub fn unknown(tokens: &[String], original: &[String]) -> ServiceCommand {
    let words = tokens.iter().map(String::as_str).collect::<Vec<_>>();
    let positions = Positions::new(original, tokens);
    let rejection = unknown_command(&words, &positions);
    ServiceCommand::Invalid(Box::new(invalid_command(rejection, tokens)))
}

/// Whether `tail` holds `cli` followed by a manifest command noun (a service
/// or account command, not a runner operation such as `doctor`).
fn manifest_command_follows(tail: &[String]) -> bool {
    tail.iter()
        .position(|token| token == "cli")
        .and_then(|at| tail.get(at + 1))
        .is_some_and(|noun| {
            operations()
                .iter()
                .any(|operation| command_words(operation).first() == Some(&noun.as_str()))
        })
}

/// An `--output` value before `cli` that is not an output mode
/// (`--output yaml cli run list`). The error suggests the
/// nearest mode, or `json` when none is near, and is rendered by the service
/// renderer. `None` unless `cli` and a manifest command follow: runner
/// operations (`cli doctor`) keep the runner renderer.
pub fn invalid_global_output(args: &[String], index: usize) -> Option<ServiceCommand> {
    const MODES: [&str; 3] = ["human", "json", "jsonl"];
    let token = args.get(index)?;
    let (value, value_at, inline) = match token.split_once('=') {
        Some(("--output", value)) => (value.to_owned(), index, true),
        None if token == "--output" => (args.get(index + 1)?.clone(), index + 1, false),
        _ => return None,
    };
    if value.is_empty()
        || OutputMode::parse(&value).is_ok()
        || !manifest_command_follows(&args[value_at + 1..])
    {
        return None;
    }
    let lower = value.to_ascii_lowercase();
    let near = MODES
        .into_iter()
        .find(|mode| *mode == lower)
        .or_else(|| did_you_mean(&lower, MODES));
    let mode = near.unwrap_or("json");
    let mut corrected = args.to_vec();
    corrected[value_at] = if inline {
        format!("--output={mode}")
    } else {
        mode.to_owned()
    };
    let expected = format!(
        "invalid output mode {value_quoted}; expected human, json, or jsonl",
        value_quoted = crate::error::quote(&value)
    );
    let rejection = match near {
        Some(mode) => reject(
            format!("{expected}; did you mean `{mode}`?"),
            Correction::Argv {
                action: format!("Use --output {mode}: `{{command}}`"),
                argv: corrected,
            },
        ),
        None => reject(
            expected,
            Correction::Argv {
                action: "Use --output json for one JSON document (jsonl and human are the other modes): `{command}`".to_owned(),
                argv: corrected,
            },
        ),
    };
    Some(ServiceCommand::Invalid(Box::new(invalid_command(
        rejection,
        &args[value_at + 1..],
    ))))
}

/// The removed service-selection option, spelled in two parts so the
/// public binary carries no trace of it; only a person who types it sees it
/// named back (identical in both ports).
const REMOVED_OPTION: [&str; 2] = ["--service-", "environment"];

/// An option neither the runner nor any service command knows, spelled
/// before `cli` and a manifest command (`--colour always cli
/// service status`). It is rejected with INVOCATION_INVALID naming the
/// option, never forwarded to the language: the suggested argv drops it
/// (and the one value word that sits between it and `cli`).
pub fn unknown_option_before_cli(
    args: &[String],
    index: usize,
    global_kind: impl Fn(&str) -> Option<bool>,
) -> Option<ServiceCommand> {
    // The width of a known option token (with its value), or `None`.
    let known = |token: &str| {
        let (name, inline) = match token.split_once('=') {
            Some((name, _)) if name.starts_with("--") => (name, true),
            _ => (token, false),
        };
        global_kind(name)
            .or_else(|| {
                inference()["optionConversions"][name]
                    .is_object()
                    .then_some(true)
            })
            .or_else(|| local_option(name).map(|(_, value)| value))
            .map(|takes_value| if takes_value && !inline { 2 } else { 1 })
    };
    let option = |token: &String| token != "--" && token.starts_with('-');
    let mut at = index;
    let unknown = loop {
        let token = args.get(at).filter(|token| option(token))?;
        match known(token) {
            Some(width) => at += width,
            None => break at,
        }
    };
    let token = &args[unknown];
    let name = token
        .split_once('=')
        .filter(|(name, _)| name.starts_with("--"))
        .map_or(token.as_str(), |(name, _)| name)
        .to_owned();
    // The unknown option may take one value word.
    let mut end = unknown + 1;
    if !token.contains('=')
        && args
            .get(end)
            .is_some_and(|next| next != "cli" && !option(next))
    {
        end += 1;
    }
    let mut cli_at = end;
    while args.get(cli_at).is_some_and(|next| next != "cli") {
        cli_at += known(args.get(cli_at).filter(|next| option(next))?)?;
    }
    if !manifest_command_follows(args.get(cli_at..)?) {
        return None;
    }
    let mut corrected = args[..unknown].to_vec();
    corrected.extend_from_slice(&args[end..]);
    let rejection = if name.strip_prefix(REMOVED_OPTION[0]) == Some(REMOVED_OPTION[1]) {
        reject(
            format!("unknown option {name} before `cli`; the option was removed"),
            Correction::Argv {
                action: format!(
                    "The {name} option was removed; public builds always use the OpenProse production service. Run the command without it: `{{command}}`"
                ),
                argv: corrected,
            },
        )
    } else {
        reject(
            format!("unknown option {name} before `cli`"),
            Correction::Argv {
                action: "Remove the unknown option: `{command}`".to_owned(),
                argv: corrected,
            },
        )
    };
    Some(ServiceCommand::Invalid(Box::new(invalid_command(
        rejection,
        &args[end..],
    ))))
}

/// The canonical name of a command-local option spelled before `cli`, and
/// whether it takes a value: a common flag (`--json`, `--yes`, `--preview`),
/// any operation option, or an `optionAliases` spelling of one (`-j`, `-y`).
/// `None` for a runner-global option, an alias of one, `--help` and unknown
/// words.
fn local_option(name: &str) -> Option<(&'static str, bool)> {
    let exact = operations().iter().any(|operation| {
        operation["options"]
            .as_array()
            .is_some_and(|options| options.iter().any(|option| option["name"] == name))
    });
    let canonical = inference()["optionAliases"][name]
        .as_array()
        .filter(|_| !exact)
        .and_then(|targets| targets.first())
        .and_then(Value::as_str)
        .unwrap_or(name);
    let global = manifest()["grammar"]["globalOptions"]
        .as_array()
        .is_some_and(|globals| globals.iter().any(|global| global == canonical));
    if global || canonical == "--help" {
        return None;
    }
    let common = manifest()["grammar"]["commonOptions"]
        .as_array()
        .into_iter()
        .flatten();
    let specific = operations()
        .iter()
        .flat_map(|operation| operation["options"].as_array().into_iter().flatten());
    common
        .chain(specific)
        .find(|option| option["name"] == canonical)
        .and_then(|option| Some((option["name"].as_str()?, !option["value"].is_null())))
}

/// Command-local options before `cli` (`prose --json cli run list`,
/// `prose -y cli run cancel RUN_ID`). `index` is the first
/// token the runner-global parser did not consume. The argv is never
/// forwarded to the language: the rejection's `suggestedArgv` moves the
/// options (by their canonical names) after the command path and keeps real
/// runner globals where they were. `None` unless every option-looking token
/// up to `cli` is a runner global or a command-local option.
pub fn misplaced_local_options(
    args: &[String],
    index: usize,
    global_kind: impl Fn(&str) -> Option<bool>,
) -> Option<ServiceCommand> {
    let mut kept = args.get(..index)?.to_vec();
    let mut moved: Vec<String> = Vec::new();
    let mut named: Vec<String> = Vec::new();
    let mut canonical_names: Vec<&str> = Vec::new();
    let mut at = index;
    while let Some(token) = args.get(at) {
        if token == "cli" || token == "--" || !token.starts_with('-') {
            break;
        }
        let (name, inline) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value)),
            _ => (token.as_str(), None),
        };
        if let Some(takes_value) = global_kind(name) {
            let width = if takes_value && inline.is_none() {
                2
            } else {
                1
            };
            kept.extend_from_slice(args.get(at..at + width)?);
            at += width;
            continue;
        }
        // A unit-changing spelling (`--amount 5`) moves as its
        // target with the converted value; a value that is not converted
        // moves as typed, and the settled correction states the unit.
        let conversion = &inference()["optionConversions"][name];
        if let (Some(target), Some(multiplier)) = (
            conversion["option"].as_str(),
            conversion["multiplier"].as_u64(),
        ) {
            let value = if let Some(value) = inline {
                value.to_owned()
            } else {
                at += 1;
                args.get(at)?.clone()
            };
            at += 1;
            if let Some(converted) = converted_value(&value, multiplier) {
                moved.extend([target.to_owned(), converted.clone()]);
                named.push(format!(
                    "`{name} {value}` ({target} {converted}, {} converted to {})",
                    conversion["from"].as_str().unwrap_or_default(),
                    conversion["to"].as_str().unwrap_or_default()
                ));
                canonical_names.push(target);
            } else {
                moved.extend([name.to_owned(), value]);
                named.push(format!("`{name}`"));
                canonical_names.push(conversion_source(name)?);
            }
            continue;
        }
        let (canonical, takes_value) = local_option(name)?;
        match (takes_value, inline) {
            (true, Some(value)) => moved.push(format!("{canonical}={value}")),
            (true, None) => {
                moved.push(canonical.to_owned());
                moved.push(args.get(at + 1)?.clone());
                at += 1;
            }
            (false, None) => moved.push(canonical.to_owned()),
            (false, Some(_)) => return None,
        }
        at += 1;
        named.push(if name == canonical {
            format!("`{name}`")
        } else {
            format!("`{name}` ({canonical})")
        });
        canonical_names.push(canonical);
    }
    if moved.is_empty() || args.get(at).is_none_or(|token| token != "cli") {
        return None;
    }
    let rest = &args[at..];
    let split = rest
        .iter()
        .position(|token| token == "--")
        .unwrap_or(rest.len());
    let mut corrected = kept;
    corrected.extend_from_slice(&rest[..split]);
    corrected.extend(moved);
    corrected.extend_from_slice(&rest[split..]);
    let verb = if named.len() == 1 {
        "belongs"
    } else {
        "belong"
    };
    let rejection = reject(
        format!(
            "{} {verb} after the command path, not before `cli`; nothing was forwarded or sent",
            named.join(", ")
        ),
        Correction::Argv {
            action: format!(
                "Move {} after the command path: `{{command}}`",
                canonical_names.join(", ")
            ),
            argv: corrected.clone(),
        },
    );
    Some(ServiceCommand::Invalid(Box::new(invalid_command(
        settle(rejection),
        &corrected[index..],
    ))))
}

/// The static spelling of an `optionConversions` source option.
fn conversion_source(name: &str) -> Option<&'static str> {
    inference()["optionConversions"]
        .as_object()?
        .keys()
        .find(|key| *key == name)
        .map(String::as_str)
}

/// Help is text in every output mode: a request for help
/// (`help` first, or `--help`/`-h`/a trailing `help`) that also carries
/// `--json` reads as the same request without it, so `prose help cli --json`
/// and `prose cli run --help --json` print their topic instead of being
/// forwarded or rejected. `None` when nothing changes.
pub fn help_without_json(args: &[String]) -> Option<Vec<String>> {
    let end = args
        .iter()
        .position(|token| token == "--")
        .unwrap_or(args.len());
    let head = &args[..end];
    let asks = head.first().is_some_and(|first| first == "help")
        || head.last().is_some_and(|last| last == "help")
        || head.iter().any(|token| token == "--help" || token == "-h");
    if !asks || !head.iter().any(|token| token == "--json") {
        return None;
    }
    let mut kept = head
        .iter()
        .filter(|token| *token != "--json")
        .cloned()
        .collect::<Vec<_>>();
    kept.extend_from_slice(&args[end..]);
    Some(kept)
}

/// A service command that reached the language path: the
/// words after the runner globals name a service command without `cli`
/// (`prose run list`), or a runner-global alias precedes `cli` or the
/// command (`prose --format json cli run list`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliRedirect {
    /// The corrected argv (after the product name).
    pub argv: Vec<String>,
    /// Command words that, when one names an existing file or directory,
    /// make the original a language command after all (`prose run submit`
    /// beside a file named `submit`). Empty when `cli` was given.
    pub operands: Vec<String>,
    /// The rejection to render when no operand exists on disk.
    pub command: ServiceCommand,
    /// The typed word is itself a language command (`prose status`, `prose
    /// run FILE`): the argv is forwarded, with `argv` as the
    /// `HOSTED_UNAVAILABLE` hint, unless the default hosted harness would
    /// refuse it; then `command` is rendered.
    pub language: bool,
    /// The words are always forwarded, with `argv` as the
    /// `HOSTED_UNAVAILABLE` hint (`prose run FILE`: the default hosted
    /// harness refuses it with `HOSTED_UNAVAILABLE`, and the Action names
    /// `cli run submit FILE --preview`).
    pub hint_only: bool,
}

/// Whether `word` is a current language command (SPEC 7.1,
/// `grammar.intentInference.languageCommands`).
fn is_language_command(word: &str) -> bool {
    inference()["languageCommands"]
        .as_array()
        .is_some_and(|words| words.iter().any(|known| known == word))
}

/// The one command a lone word before `cli` stands for through
/// `nounSynonyms` (`login` -> `auth login`, `models` -> `model list`): a
/// complete command path, or a group with exactly one verb. Never a guess by
/// distance, so a future language command is not captured.
fn lone_synonym(word: &str) -> Option<Vec<&'static str>> {
    let mut words = inference()["nounSynonyms"][word]
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>();
    if command_paths().iter().any(|path| path[..] == words[..]) {
        return Some(words);
    }
    match verbs_after(&words).as_slice() {
        [only] => {
            words.push(only);
            command_paths()
                .iter()
                .any(|path| path[..] == words[..])
                .then_some(words)
        }
        _ => None,
    }
}

/// How a rejected language command word before `cli` is described
/// (identical in both ports).
const LANGUAGE_WORD: &str = "is a language command, which runs only with a local harness";
const LANGUAGE_TAIL: &str =
    "To run the language command, select a local harness with `prose cli harness use <id>`";
const FORWARD_TAIL: &str =
    "To pass these words to the OpenProse language instead, put `--` before them";

fn rest_owned(words: &[&str]) -> Vec<String> {
    words.iter().map(|word| (*word).to_owned()).collect()
}

/// The command a lone word before `cli` and the word after it name through
/// `nounSynonyms`, and how many typed words that spans: [`lone_synonym`],
/// whose verb a typed sibling verb replaces (`credits topup` -> `wallet
/// topup`), or a group synonym with one of its verbs typed (`organization
/// list` -> `org list`).
fn lone_command<'a>(noun: &str, verb: Option<&'a str>) -> Option<(Vec<&'a str>, usize)> {
    if let Some(found) = lone_synonym(noun) {
        return Some(synonym_path(&found, verb));
    }
    let listed = inference()["nounSynonyms"][noun]
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<&'static str>>();
    let verb = verb?;
    if is_command_path(&listed) {
        return None;
    }
    let mut path: Vec<&'a str> = listed;
    path.push(verb);
    is_command_path(&path).then_some((path, 2))
}

/// A service word before `cli` that is no command path and no lone synonym:
/// a verb before its group (`list jobs`), a `commandRewrites` phrase
/// (`stop`, `delete`, `share`, `cron`), `help [COMMAND]`, or `run FILE`,
/// which is `cli run submit FILE --preview`. `prefix` is the runner globals
/// before the words. `help` and `run` are language commands: the caller
/// forwards them unless the hosted harness would refuse.
fn service_word(prefix: &[String], rest: &[String]) -> Option<CliRedirect> {
    let words = rest.iter().map(String::as_str).collect::<Vec<_>>();
    let noun = *words.first()?;
    let mut original = prefix.to_vec();
    original.push("cli".to_owned());
    original.extend_from_slice(rest);
    let positions = Positions::new(&original, rest);
    let (meant, operands) = match words.as_slice() {
        ["help", tail @ ..] => {
            let mut path: Vec<&str> = Vec::new();
            for word in tail {
                let mut candidate = path.clone();
                candidate.push(word);
                if word.starts_with('-') || !is_command_prefix(&candidate) {
                    break;
                }
                path = candidate;
            }
            let mut argv = prefix.to_vec();
            if !path.is_empty() {
                argv.push("cli".to_owned());
                argv.extend(path.iter().map(|word| (*word).to_owned()));
            }
            argv.push("--help".to_owned());
            let action = if path.is_empty() {
                "Show the runner help: `{command}`".to_owned()
            } else {
                format!("Show the help of `cli {}`: `{{command}}`", path.join(" "))
            };
            (
                Meaning {
                    rejection: reject("", Correction::Argv { action, argv }),
                    typed: 1 + path.len(),
                    why: None,
                },
                vec!["help".to_owned()],
            )
        }
        ["run", file, tail @ ..] if !file.starts_with('-') => {
            let stop = tail
                .iter()
                .position(|token| *token == "--")
                .unwrap_or(tail.len());
            let mut argv = prefix.to_vec();
            argv.extend(["cli", "run", "submit", file].map(str::to_owned));
            argv.extend(tail[..stop].iter().map(|word| (*word).to_owned()));
            if !tail[..stop].contains(&"--preview") {
                argv.push("--preview".to_owned());
            }
            argv.extend(tail[stop..].iter().map(|word| (*word).to_owned()));
            (
                Meaning {
                    rejection: settle(reject(
                        "",
                        Correction::Argv {
                            action: "Use `cli run submit`, previewing the run first: `{command}`"
                                .to_owned(),
                            argv,
                        },
                    )),
                    typed: 2,
                    why: None,
                },
                vec![(*file).to_owned()],
            )
        }
        _ => {
            let meant = phrase_meaning(&words, &positions, 2)
                .or_else(|| verb_first(&words, &positions))
                .or_else(|| phrase_meaning(&words, &positions, 1))?;
            let operands = rest[..meant.typed].to_vec();
            (
                Meaning {
                    rejection: settle(meant.rejection),
                    ..meant
                },
                operands,
            )
        }
    };
    let (action, argv) = match meant.rejection.correction {
        Correction::Argv { action, argv } => (action, argv),
        Correction::Command { action, words } => {
            let mut argv = prefix.to_vec();
            argv.push("cli".to_owned());
            argv.extend(words);
            (action, argv)
        }
        Correction::Text(_) => return None,
    };
    let language = is_language_command(noun);
    let typed = words[..meant.typed].join(" ");
    let why = meant
        .why
        .map(|why| {
            let mut chars = why.chars();
            let head = chars
                .next()
                .map(|head| head.to_uppercase().collect::<String>())
                .unwrap_or_default();
            format!(" {head}{}.", chars.as_str())
        })
        .unwrap_or_default();
    let text = render::argv_text(&argv);
    let reason = if language {
        format!(
            "`{typed}` {LANGUAGE_WORD}; did you mean `{text}`? Nothing was forwarded or sent. {LANGUAGE_TAIL}"
        )
    } else {
        format!(
            "`{typed}` is not a command; did you mean `{text}`?{why} Nothing was forwarded or sent. {FORWARD_TAIL}"
        )
    };
    let rejection = reject(
        reason,
        Correction::Argv {
            action,
            argv: argv.clone(),
        },
    );
    Some(CliRedirect {
        argv,
        operands,
        command: ServiceCommand::Invalid(Box::new(invalid_command(rejection, rest))),
        language,
        hint_only: noun == "run",
    })
}

/// Whether `globals` (the runner options before the command words) only
/// choose how output looks (`--output MODE`, `--no-color`, `--verbose`), so
/// no local harness run was asked for.
fn only_display_globals(globals: &[String]) -> bool {
    let mut index = 0;
    while let Some(token) = globals.get(index) {
        index += match token.as_str() {
            "--output" => 2,
            "--no-color" | "--verbose" => 1,
            other if other.starts_with("--output=") => 1,
            _ => return false,
        };
    }
    true
}

/// The runner-global option an alias before `cli` stands for: an
/// `optionAliases` entry whose target is a `grammar.globalOptions` name.
fn global_alias(name: &str) -> Option<&'static str> {
    let globals = manifest()["grammar"]["globalOptions"].as_array()?;
    inference()["optionAliases"][name]
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .find(|candidate| globals.iter().any(|global| global == candidate))
}

/// Detects a missing `cli` or a runner-global alias at `index`, the first
/// token the runner-global parser did not consume. `global_kind` classifies
/// real runner globals: `Some(true)` takes a value, `Some(false)` is a flag.
/// Pure: the caller checks `operands` against the filesystem and forwards
/// (with `argv` as a hint) when one exists. `None` leaves the argv to the
/// ordinary parser.
pub fn cli_redirect(
    args: &[String],
    index: usize,
    global_kind: impl Fn(&str) -> Option<bool>,
) -> Option<CliRedirect> {
    let mut corrected = args.get(..index)?.to_vec();
    let mut replaced: Vec<String> = Vec::new();
    let mut reasons: Vec<String> = Vec::new();
    let mut at = index;
    while let Some(token) = args.get(at) {
        if token == "cli" || token == "--" {
            break;
        }
        let (name, inline) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value)),
            _ => (token.as_str(), None),
        };
        if let Some(target) = global_alias(name) {
            let (value, width) = match inline {
                Some(value) => (value.to_owned(), 1),
                None => (args.get(at + 1)?.clone(), 2),
            };
            if inline.is_some() {
                corrected.push(format!("{target}={value}"));
            } else {
                corrected.push(target.to_owned());
                corrected.push(value);
            }
            replaced.push(format!("{name} with {target}"));
            reasons.push(format!(
                "`{name}` is not a runner option; did you mean {target}?"
            ));
            at += width;
            continue;
        }
        // A real runner global after an alias keeps its place.
        if !replaced.is_empty() {
            match global_kind(name) {
                Some(true) => {
                    let width = if inline.is_some() { 1 } else { 2 };
                    corrected.extend_from_slice(args.get(at..at + width)?);
                    at += width;
                    continue;
                }
                Some(false) if inline.is_none() => {
                    corrected.push(token.clone());
                    at += 1;
                    continue;
                }
                _ => {}
            }
        }
        break;
    }
    let rest = &args[at..];
    let words = rest.iter().map(String::as_str).collect::<Vec<_>>();
    let mut language = false;
    let mut synonym: Option<String> = None;
    // A whole command line typed as one word (`prose "cli run list"`).
    if replaced.is_empty() {
        if let Some(split) = words.first().and_then(|word| unsplit_words(word)) {
            corrected.extend(split.iter().map(|word| (*word).to_owned()));
            if split.first() != Some(&"cli") {
                corrected.insert(index, "cli".to_owned());
            }
            corrected.extend_from_slice(&rest[1..]);
            let rejection = reject(
                format!(
                    "the word {} holds a whole command line; the argv looks unsplit (quoted, so the shell passed it as one word): did you mean `{}`? Nothing was forwarded or sent",
                    crate::error::quote(words[0]),
                    render::argv_text(&corrected)
                ),
                Correction::Argv {
                    action: "Pass each word separately: `{command}`".to_owned(),
                    argv: corrected.clone(),
                },
            );
            return Some(CliRedirect {
                argv: corrected,
                operands: vec![words[0].to_owned()],
                command: ServiceCommand::Invalid(Box::new(invalid_command(rejection, rest))),
                language: false,
                hint_only: false,
            });
        }
    }
    let (tokens, operands) = if words.first() == Some(&"cli") {
        if replaced.is_empty() {
            return None;
        }
        corrected.extend_from_slice(rest);
        (&rest[1..], Vec::new())
    } else {
        let (command, typed) = match words.as_slice() {
            // A bare `run` with no runner option selecting a harness runs
            // nothing here: hosted runs are `cli run submit`.
            ["run"] if only_display_globals(&args[..index]) => {
                synonym = Some("run submit".to_owned());
                (vec!["run", "submit", "--help"], 1)
            }
            [noun, verb, ..] if !verb.starts_with('-') && is_command_prefix(&[*noun, *verb]) => {
                (vec![*noun, *verb], 2)
            }
            [noun, ..] if command_paths().iter().any(|path| path[..] == [*noun]) => {
                (vec![*noun], 1)
            }
            [word, rest @ ..] => {
                // `prose list jobs`: a verb before its group is never a lone
                // synonym.
                let original = corrected
                    .iter()
                    .cloned()
                    .chain(std::iter::once("cli".to_owned()))
                    .chain(rest_owned(&words))
                    .collect::<Vec<_>>();
                let verb_before =
                    verb_first(&words, &Positions::new(&original, &rest_owned(&words))).is_some();
                let Some((command, typed)) = (!verb_before)
                    .then(|| lone_command(word, rest.first().copied()))
                    .flatten()
                else {
                    return if replaced.is_empty() {
                        service_word(&corrected, &args[at..])
                    } else {
                        None
                    };
                };
                language = is_language_command(word);
                synonym = Some(command.join(" "));
                (command, typed)
            }
            [] => return None,
        };
        corrected.push("cli".to_owned());
        corrected.extend(command.iter().map(|word| (*word).to_owned()));
        corrected.extend_from_slice(&rest[typed..]);
        let spelled = words[..typed].join(" ");
        let kind = if words.as_slice() == ["run"] {
            "alone runs nothing here: hosted runs are `prose cli run submit FILE`"
        } else if language {
            LANGUAGE_WORD
        } else if synonym.is_some() {
            "is not a command"
        } else {
            "is a service command"
        };
        let tail = if language {
            LANGUAGE_TAIL
        } else {
            FORWARD_TAIL
        };
        reasons.push(format!(
            "`{spelled}` {kind}; did you mean `{}`? Nothing was forwarded or sent. {tail}",
            render::argv_text(&corrected)
        ));
        (
            rest,
            words[..typed]
                .iter()
                .map(|word| (*word).to_owned())
                .collect(),
        )
    };
    let fix = match &synonym {
        Some(command) => format!("use `cli {command}`"),
        None => "insert cli before the service command".to_owned(),
    };
    let action = match (replaced.is_empty(), operands.is_empty()) {
        (true, _) => {
            let mut fix = fix;
            fix[..1].make_ascii_uppercase();
            format!("{fix}: `{{command}}`")
        }
        (false, true) => format!("Replace {}: `{{command}}`", replaced.join(", ")),
        (false, false) => format!("Replace {} and {fix}: `{{command}}`", replaced.join(", ")),
    };
    let rejection = settle(reject(
        reasons.join(" "),
        Correction::Argv {
            action,
            argv: corrected.clone(),
        },
    ));
    // A rejected --output spelling before `cli` (`--format json`) chooses how
    // the error is printed.
    let mut scanned = args[index..at].to_vec();
    scanned.extend_from_slice(tokens);
    Some(CliRedirect {
        argv: corrected,
        operands,
        command: ServiceCommand::Invalid(Box::new(invalid_command(rejection, &scanned))),
        language,
        hint_only: false,
    })
}

/// The `HOSTED_UNAVAILABLE` refusal of a forwarded argv that also reads as a
/// service command (a file named like the command word exists): the Action
/// names the exact `prose cli ...` command as well, and
/// `details.suggestedArgv` carries it. The refusal of `prose run FILE` keeps
/// its frozen Action (`details_only`); only `details.suggestedArgv` names
/// `cli run submit FILE --preview`.
#[must_use]
pub fn with_cli_hint(
    mut error: RunnerError,
    hint: Option<&[String]>,
    details_only: bool,
) -> RunnerError {
    let Some(argv) = hint else {
        return error;
    };
    if !details_only {
        error.action = format!(
            "To use the hosted service, run `{}`; running programs on this machine needs a local harness (`{} cli harness list`).",
            render::argv_text(argv),
            render::product()
        );
    }
    error.with_detail("suggestedArgv", json!(argv))
}

/// `--help` counts only in an option position, never as an option's value or
/// after `--`.
fn help_position(operation: &Value, rest: &[String]) -> bool {
    let value_options = operation["options"]
        .as_array()
        .map(|options| {
            options
                .iter()
                .filter(|option| !option["value"].is_null())
                .filter_map(|option| option["name"].as_str())
                .collect::<BTreeSet<_>>()
        })
        .unwrap_or_default();
    let mut index = 0;
    while let Some(token) = rest.get(index) {
        if token == "--" {
            return false;
        }
        if token == "--help" || token == "-h" {
            return true;
        }
        if value_options.contains(token.as_str()) || token == "--output" {
            index += 2;
            continue;
        }
        index += 1;
    }
    false
}

fn unknown_command(tokens: &[&str], positions: &Positions<'_>) -> Rejection {
    settle(unknown_command_unsettled(tokens, positions))
}

/// The correction for a corrected command path: the argv
/// with `len` tokens at `at` replaced by the completed `path`, the help of
/// a group that still needs one of several verbs, or the help of a bare
/// command that declares only optional arguments (its handler may need one,
/// as `cli run submit` needs a program).
fn path_correction(
    path: Vec<&str>,
    at: usize,
    len: usize,
    tokens: &[&str],
    positions: &Positions<'_>,
) -> (String, Correction) {
    let rest = tokens.get(at + len..).unwrap_or_default();
    let Some((full, consumed)) = complete_path(path.clone(), rest) else {
        let spelled = path.join(" ");
        return (
            spelled.clone(),
            Correction::Command {
                action: format!(
                    "Use `cli {spelled}` with one of its commands; `{{command}}` describes them"
                ),
                words: help_words(&path),
            },
        );
    };
    // The words the user did not type verbatim: shown in the reason.
    let shown = full[..full.len() - consumed]
        .iter()
        .copied()
        .chain(
            full[full.len() - consumed..]
                .iter()
                .zip(rest)
                .filter(|(word, typed)| *word != *typed)
                .map(|(word, _)| *word),
        )
        .collect::<Vec<_>>()
        .join(" ");
    let spelled = full.join(" ");
    let bare = rest.len() == consumed;
    let optional_only = operations()
        .iter()
        .find(|operation| command_words(operation) == full)
        .and_then(|operation| operation["arguments"].as_array())
        .is_some_and(|arguments| {
            !arguments.is_empty()
                && arguments
                    .iter()
                    .all(|argument| argument["required"] != true)
        });
    if bare && optional_only {
        let mut help = full.clone();
        help.push("--help");
        return (
            shown,
            argv_or_help(
                format!("Use `cli {spelled}`; `{{command}}` shows what it needs"),
                positions.splice(at, len + consumed, &help),
                &full,
                "`{command}` shows the syntax",
            ),
        );
    }
    (
        shown,
        argv_or_help(
            format!("Use `cli {spelled}`: `{{command}}`"),
            positions.splice(at, len + consumed, &full),
            &full,
            "`{command}` shows the syntax",
        ),
    )
}

/// What an unknown leading phrase means: the rejection, how many words it
/// spans and a rewrite's explanation.
struct Meaning {
    rejection: Rejection,
    typed: usize,
    why: Option<&'static str>,
}

/// A `commandRewrites` phrase of `len` words at the start of `tokens`. A
/// one-word phrase followed by a verb of its target's group names that
/// command instead (`cli schedule list` -> `cli job list`).
fn phrase_meaning(tokens: &[&str], positions: &Positions<'_>, len: usize) -> Option<Meaning> {
    let phrase = tokens.get(..len)?.join(" ");
    let rewrite = command_rewrite(&phrase)?;
    let group = *rewrite.command.first()?;
    if let Some(next) = tokens.get(len).copied() {
        if len == 1 && verbs_after(&[group]).contains(&next) {
            let (shown, correction) = path_correction(vec![group, next], 0, 2, tokens, positions);
            return Some(Meaning {
                rejection: reject(
                    format!("unknown command `cli {phrase} {next}`; did you mean `cli {shown}`?"),
                    correction,
                ),
                typed: 2,
                why: None,
            });
        }
    }
    let (spelled, why, correction) = rewrite_correction(&rewrite, len - 1, tokens, positions);
    Some(Meaning {
        rejection: reject(
            format!("unknown command `cli {phrase}`; did you mean `{spelled}`? {why}"),
            correction,
        ),
        typed: len,
        why: Some(why),
    })
}

/// A verb typed before its group, or before a noun synonym of it: `cli list
/// jobs` -> `cli job list`, `cli delete program SLUG` -> `cli program delete
/// SLUG`. The verb is exact or a `verbSynonyms` entry; never a guess by
/// distance.
fn verb_first(tokens: &[&str], positions: &Positions<'_>) -> Option<Meaning> {
    let (noun, typed) = (*tokens.first()?, *tokens.get(1)?);
    if typed.starts_with('-') {
        return None;
    }
    let group = if is_command_prefix(&[typed]) {
        typed
    } else {
        inference()["nounSynonyms"][typed]
            .as_array()?
            .first()?
            .as_str()?
    };
    // A noun synonym of the same group names its own command (`credits
    // topup`).
    if is_command_path(&[group])
        || !is_command_prefix(&[group])
        || inference()["nounSynonyms"][noun][0] == group
    {
        return None;
    }
    let verbs = verbs_after(&[group]);
    let verb = verbs
        .iter()
        .copied()
        .find(|verb| *verb == noun)
        .or_else(|| synonym("verbSynonyms", noun, |candidate| verbs.contains(&candidate)))?;
    let (shown, correction) = path_correction(vec![group, verb], 0, 2, tokens, positions);
    Some(Meaning {
        rejection: reject(
            format!(
                "unknown command `cli {noun} {typed}`; the command word comes first: did you mean `cli {shown}`?"
            ),
            correction,
        ),
        typed: 2,
        why: None,
    })
}

/// The command a noun synonym and the word after it name, and how many typed
/// words that spans: `runs list` repeats the synonym's verb, and a sibling
/// verb replaces it (`credits topup` -> `wallet topup`).
fn synonym_path<'a>(words: &[&'a str], next: Option<&'a str>) -> (Vec<&'a str>, usize) {
    let Some(next) = next.filter(|_| words.len() >= 2) else {
        return (words.to_vec(), 1);
    };
    if words.last() == Some(&next) {
        return (words.to_vec(), 2);
    }
    let parent = &words[..words.len() - 1];
    if is_command_path(words) && verbs_after(parent).contains(&next) {
        let mut path = parent.to_vec();
        path.push(next);
        return (path, 2);
    }
    (words.to_vec(), 1)
}

/// An unknown first word after `cli`: a whole command line typed as one
/// word, a phrase that means another command, a noun synonym (a plural names
/// its listing), the nearest noun, a verb typed before its noun (`submit run`)
/// or a verb of exactly one group (`watch RUN_ID`).
fn unknown_noun(noun: &str, tokens: &[&str], positions: &Positions<'_>) -> Rejection {
    let listing = all_nouns().join(", ");
    if let Some(words) = unsplit_words(noun) {
        let mut split = words.clone();
        if split.first() == Some(&"cli") {
            split.remove(0);
        }
        let line = split.join(" ");
        return reject(
            format!(
                "the word {noun_quoted} holds a whole command line; the argv looks unsplit (quoted, so the shell passed it as one word): did you mean `cli {line}`?",
                noun_quoted = crate::error::quote(noun)
            ),
            argv_or_help(
                "Pass each word separately: `{command}`".to_owned(),
                positions.splice(0, 1, &split),
                &[],
                "`{command}` lists the commands",
            ),
        );
    }
    // A phrase that means another command (`cli environment list`), a verb
    // typed before its noun (`cli list jobs`), then a one-word phrase
    // (`cli stop RUN_ID`).
    if let Some(meant) = phrase_meaning(tokens, positions, 2)
        .or_else(|| verb_first(tokens, positions))
        .or_else(|| phrase_meaning(tokens, positions, 1))
    {
        return meant.rejection;
    }
    if let Some(words) = suggest_noun_words(noun) {
        // `cli runs list`: the synonym already names the verb the user
        // typed; `cli credits topup`: a sibling verb replaces the synonym's.
        let (path, typed) = synonym_path(&words, tokens.get(1).copied());
        let (shown, correction) = path_correction(path, 0, typed, tokens, positions);
        return reject(
            format!(
                "unknown command `cli {noun}`; did you mean `cli {shown}`? Commands: {listing}"
            ),
            correction,
        );
    }
    // `cli submit run FILE`: a verb typed before its noun.
    if let Some(group) = tokens.get(1).copied().filter(|word| {
        !word.starts_with('-') && is_command_prefix(&[word]) && !is_command_path(&[word])
    }) {
        let verbs = verbs_after(&[group]);
        let verb = verbs
            .iter()
            .copied()
            .find(|verb| *verb == noun)
            .or_else(|| suggest_verb(noun, &verbs));
        if let Some(verb) = verb {
            let (shown, correction) = path_correction(vec![group, verb], 0, 2, tokens, positions);
            return reject(
                format!(
                    "unknown command `cli {noun} {group}`; the command word comes first: did you mean `cli {shown}`?"
                ),
                correction,
            );
        }
    }
    // `cli watch RUN_ID`: a verb of exactly one command group.
    let groups = command_paths()
        .into_iter()
        .filter(|path| path.len() == 2 && path[1] == noun)
        .map(|path| path[0])
        .collect::<BTreeSet<_>>();
    if let [group] = groups.into_iter().collect::<Vec<_>>().as_slice() {
        let (shown, correction) = path_correction(vec![group, noun], 0, 1, tokens, positions);
        return reject(
            format!(
                "unknown command `cli {noun}`; did you mean `cli {shown}`? Commands: {listing}"
            ),
            correction,
        );
    }
    reject(
        format!("unknown command `cli {noun}`. Commands: {listing}"),
        Correction::Command {
            action: "List the service commands with `{command}`".to_owned(),
            words: vec!["--help".to_owned()],
        },
    )
}

fn unknown_command_unsettled(tokens: &[&str], positions: &Positions<'_>) -> Rejection {
    let noun = tokens.first().copied().unwrap_or_default();
    // Walk the longest known group prefix, then suggest the next word.
    let mut prefix: Vec<&str> = Vec::new();
    for word in tokens {
        let mut candidate = prefix.clone();
        candidate.push(word);
        if verbs_after(&candidate).is_empty() {
            break;
        }
        prefix = candidate;
    }
    // A complete one-word runner command whose own parser rejected the rest
    // (`cli doctor extra`).
    if prefix.is_empty() && command_paths().iter().any(|path| path[..] == [noun]) {
        return reject(
            format!("missing or invalid arguments for `cli {noun}`"),
            Correction::Command {
                action: "`{command}` shows the syntax".to_owned(),
                words: help_words(&[noun]),
            },
        );
    }
    if prefix.is_empty() {
        return unknown_noun(noun, tokens, positions);
    }
    let group = prefix.join(" ");
    let verbs = verbs_after(&prefix);
    let group_help = Correction::Command {
        action: format!("Choose one of the `cli {group}` commands; `{{command}}` describes them"),
        words: help_words(&prefix),
    };
    match tokens
        .get(prefix.len())
        .filter(|word| !word.starts_with('-'))
    {
        None => {
            let reason = format!("`cli {group}` needs a command: {}", verbs.join(", "));
            match verbs.as_slice() {
                // A group with one verb completes to it (`cli model` -> `cli model list`).
                [_] => reject(
                    reason,
                    path_correction(prefix.clone(), 0, prefix.len(), tokens, positions).1,
                ),
                _ => reject(reason, group_help),
            }
        }
        // A complete runner or service command whose own parser rejected
        // the rest (`cli cleanup prime` without a handle).
        Some(word) if verbs.contains(word) => {
            let mut path = prefix.clone();
            path.push(word);
            reject(
                format!("missing or invalid arguments for `cli {}`", path.join(" ")),
                Correction::Command {
                    action: "`{command}` shows the syntax".to_owned(),
                    words: help_words(&path),
                },
            )
        }
        Some(word) => {
            // A phrase that means another command (`cli run result RUN_ID`).
            if let Some(rewrite) = command_rewrite(&format!("{group} {word}")) {
                return rewrite_rejection(
                    &format!("{group} {word}"),
                    &rewrite,
                    prefix.len(),
                    tokens,
                    positions,
                );
            }
            let found = suggest_verb(word, &verbs);
            let hint = found.map_or_else(
                || ".".to_owned(),
                |verb| format!("; did you mean `cli {group} {verb}`?"),
            );
            let correction = match found {
                Some(verb) => {
                    let mut path = prefix.clone();
                    path.push(verb);
                    path_correction(path, 0, prefix.len() + 1, tokens, positions).1
                }
                None => group_help,
            };
            reject(
                format!(
                    "unknown command `cli {group} {word}`{hint} Commands: {}",
                    verbs.join(", ")
                ),
                correction,
            )
        }
    }
}

/// `commandRewrites`: `cli run result RUN_ID` is
/// `cli run show RUN_ID --file outputs/result.json`, and `cli program run
/// SLUG` is `cli run submit --from SLUG` (`argumentOption`). `typed` is the
/// phrase as typed; its last word is token `at`. Without the target's
/// required arguments the suggestion is the target's help.
fn rewrite_rejection(
    typed: &str,
    rewrite: &Rewrite,
    at: usize,
    tokens: &[&str],
    positions: &Positions<'_>,
) -> Rejection {
    let (spelled, why, correction) = rewrite_correction(rewrite, at, tokens, positions);
    reject(
        format!("unknown command `cli {typed}`; did you mean `{spelled}`? {why}"),
        correction,
    )
}

/// The correction of a `commandRewrites` phrase whose last word is token
/// `at`: the spelled target, the reason's explanation and the correction. A
/// typed positional argument selects `argumentCommand` when there is one
/// (`cli why RUN_ID` -> `cli run show RUN_ID`); a target that appends
/// `--help` drops the rest of the line; `note` starts the Action.
fn rewrite_correction(
    chosen: &Rewrite,
    at: usize,
    tokens: &[&str],
    positions: &Positions<'_>,
) -> (String, &'static str, Correction) {
    let rest = tokens.get(at + 1..).unwrap_or_default();
    let owned_rest = rest
        .iter()
        .map(|word| (*word).to_owned())
        .collect::<Vec<_>>();
    let mut rewrite = chosen.clone();
    if let Some(alternative) = &chosen.argument_command {
        let operation = operations()
            .iter()
            .find(|operation| command_words(operation) == *alternative);
        if first_positional(operation, &owned_rest).is_some() {
            rewrite.command.clone_from(alternative);
            rewrite.append = Vec::new();
            rewrite.argument_option = None;
        }
    }
    let command = &rewrite.command;
    let target = command.join(" ");
    let operation = operations()
        .iter()
        .find(|operation| command_words(operation) == *command);
    let arguments = operation
        .and_then(|operation| operation["arguments"].as_array())
        .cloned()
        .unwrap_or_default();
    let required = arguments
        .iter()
        .filter(|argument| argument["required"] == true)
        .count();
    let help = rewrite.append.contains(&"--help");
    let placeholders = if help {
        String::new()
    } else {
        arguments
            .iter()
            .filter(|argument| argument["required"] == true)
            .filter_map(|argument| argument["name"].as_str())
            .map(|name| format!(" {name}"))
            .collect::<String>()
    };
    let option = rewrite
        .argument_option
        .map(|option| {
            let value = operation
                .and_then(|operation| operation["options"].as_array())
                .and_then(|options| options.iter().find(|entry| entry["name"] == option))
                .and_then(|entry| entry["value"].as_str())
                .unwrap_or("VALUE");
            format!(" {option} {value}")
        })
        .unwrap_or_default();
    let appended = rewrite
        .append
        .iter()
        .map(|word| format!(" {word}"))
        .collect::<String>();
    let spelled = format!("cli {target}{placeholders}{option}{appended}");
    let given = positional_count(operation, rest);
    let needed = required + usize::from(rewrite.argument_option.is_some());
    // A bare target whose arguments are all optional still needs one (`cli
    // run quote` needs a program): its help.
    let optional_only = !arguments.is_empty()
        && arguments
            .iter()
            .all(|argument| argument["required"] != true);
    let bare_optional = optional_only && given == 0 && rewrite.argument_option.is_none();
    let corrected = if help {
        let mut with = command.clone();
        with.extend(rewrite.append.iter().copied());
        positions.splice(0, tokens.len(), &with)
    } else if given >= needed && !bare_optional {
        positions.splice(0, at + 1, command).map(|mut argv| {
            let start = argv.len() - rest.len();
            if let Some(option) = rewrite.argument_option {
                // The first positional becomes the option's value.
                if let Some(first) = first_positional(operation, &argv[start..]) {
                    argv.insert(start + first, option.to_owned());
                }
            }
            let end = argv
                .iter()
                .position(|token| token == "--")
                .unwrap_or(argv.len());
            argv.splice(
                end..end,
                rewrite.append.iter().map(|word| (*word).to_owned()),
            );
            argv
        })
    } else {
        None
    };
    let note = rewrite
        .note
        .map(|note| format!("{note}. "))
        .unwrap_or_default();
    let correction = match corrected {
        Some(argv) => Correction::Argv {
            action: format!("{note}Use `cli {target}`: `{{command}}`"),
            argv,
        },
        None => Correction::Command {
            action: format!("{note}Use `{spelled}`; `{{command}}` shows its arguments"),
            words: help_words(command),
        },
    };
    (spelled, rewrite.why, correction)
}

/// The index of the first positional argument in `rest` for `operation`
/// (skipping options and their values).
fn first_positional(operation: Option<&Value>, rest: &[String]) -> Option<usize> {
    let takes_value = |name: &str| {
        name == "--output"
            || operation
                .and_then(|operation| operation["options"].as_array())
                .is_some_and(|options| {
                    options
                        .iter()
                        .any(|option| option["name"] == name && !option["value"].is_null())
                })
    };
    let mut index = 0;
    while let Some(token) = rest.get(index) {
        if token == "--" {
            return rest.get(index + 1).map(|_| index + 1);
        }
        if token.starts_with('-') && token != "-" {
            index += if !token.contains('=') && takes_value(token) {
                2
            } else {
                1
            };
            continue;
        }
        return Some(index);
    }
    None
}

/// How many positional arguments `rest` gives an operation (tokens that are
/// neither options nor the values of its value options or the service
/// globals).
fn positional_count(operation: Option<&Value>, rest: &[&str]) -> usize {
    let takes_value = |name: &str| {
        name == "--output"
            || operation
                .and_then(|operation| operation["options"].as_array())
                .is_some_and(|options| {
                    options
                        .iter()
                        .any(|option| option["name"] == name && !option["value"].is_null())
                })
    };
    let mut count = 0;
    let mut index = 0;
    while let Some(token) = rest.get(index) {
        index += 1;
        if *token == "--" {
            count += rest.len() - index;
            break;
        }
        if token.starts_with('-') && *token != "-" {
            if !token.contains('=') && takes_value(token) {
                index += 1;
            }
            continue;
        }
        count += 1;
    }
    count
}

/// A parser correction is complete. When a corrected argv
/// names a service command the grammar would still reject (`cli run shw`
/// becomes `cli run show` without its `RUN_ID`), the correction becomes the
/// one that second rejection carries, so `details.suggestedArgv` is never
/// itself an invocation error the grammar can see.
fn settle(rejection: Rejection) -> Rejection {
    // Each step may leave another fix (moving an option, then removing its
    // stray value): settle until the suggestion parses, at most four steps.
    let mut current = rejection;
    for _ in 0..4 {
        let before = match &current.correction {
            Correction::Argv { argv, .. } => argv.clone(),
            _ => break,
        };
        current = settle_once(current);
        match &current.correction {
            Correction::Argv { argv, .. } if *argv != before => {}
            _ => break,
        }
    }
    current
}

/// One step of [`settle`].
fn settle_once(rejection: Rejection) -> Rejection {
    let Correction::Argv { action, argv } = &rejection.correction else {
        return rejection;
    };
    let Some(cli) = argv.iter().position(|token| token == "cli") else {
        return rejection;
    };
    let args = &argv[cli + 1..];
    if !claims(args) {
        return rejection;
    }
    let next = match parse_unsettled(args, argv) {
        // A suggestion that asks for help is the command's help alone: `-h`
        // anywhere becomes `--help` after the command path, and the rest of
        // the line is dropped.
        Ok(ServiceCommand::Help(_)) => {
            let mut path: Vec<&str> = Vec::new();
            for word in args {
                let mut candidate = path.clone();
                candidate.push(word);
                if word.starts_with('-') || !is_command_prefix(&candidate) {
                    break;
                }
                path = candidate;
            }
            let mut help = argv[..=cli].to_vec();
            help.extend(path.iter().map(|word| (*word).to_owned()));
            help.push("--help".to_owned());
            if help == *argv {
                return rejection;
            }
            return Rejection {
                correction: Correction::Argv {
                    action: action.clone(),
                    argv: help,
                },
                error: rejection.error,
            };
        }
        Ok(ServiceCommand::Invoke(invocation)) if invocation.error.is_some() => {
            invocation.correction
        }
        Ok(ServiceCommand::Invalid(invalid)) => invalid.correction,
        _ => None,
    };
    let Some(next) = next else {
        return rejection;
    };
    let first = action
        .strip_suffix(": `{command}`")
        .unwrap_or(action)
        .to_owned();
    let then = |text: &str| {
        let mut chars = text.chars();
        let lowered = chars
            .next()
            .map(|head| head.to_lowercase().chain(chars).collect::<String>())
            .unwrap_or_default();
        format!("{first}, then {lowered}")
    };
    let correction = match next {
        Correction::Argv { action, argv } => Correction::Argv {
            action: then(&action),
            argv,
        },
        Correction::Command { action, words } => Correction::Command {
            action: then(&action),
            words,
        },
        Correction::Text(text) => Correction::Text(then(&text)),
    };
    Rejection {
        error: rejection.error,
        correction,
    }
}

#[allow(clippy::too_many_lines, clippy::result_large_err)]
fn parse_operation_arguments(
    operation: &Value,
    rest: &[String],
    invocation: &mut ServiceInvocation,
    positions: &Positions<'_>,
) -> Result<(), Rejection> {
    let words = command_words(operation);
    let command = format!("cli {}", words.join(" "));
    let options = operation["options"].as_array().cloned().unwrap_or_default();
    let arguments = operation["arguments"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let known = |name: &str| options.iter().find(|option| option["name"] == name);
    let mut positionals: Vec<(usize, String)> = Vec::new();
    let mut json_at = None;
    let mut preview_at = None;
    let mut index = 0;
    let mut literal = false;
    while let Some(token) = rest.get(index) {
        let at = index;
        index += 1;
        if literal || token == "-" || !token.starts_with('-') {
            positionals.push((at, token.clone()));
            continue;
        }
        if token == "--" {
            literal = true;
            continue;
        }
        let (name, inline) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value)),
            _ => (token.as_str(), None),
        };
        // The width of this option occurrence in tokens (for removal).
        let width = if inline.is_some() { 1 } else { 2 };
        let take_value = |index: &mut usize| -> Result<String, Rejection> {
            if let Some(value) = inline {
                return Ok(value.to_owned());
            }
            let value = rest.get(*index).ok_or_else(|| {
                reject(
                    format!("option {name} requires a value"),
                    Correction::Command {
                        action: format!(
                            "Give {name} a value; `{{command}}` shows the syntax of `{command}`"
                        ),
                        words: help_words(&words),
                    },
                )
            })?;
            *index += 1;
            Ok(value.clone())
        };
        let once = |len: usize| {
            argv_or_help(
                format!("Pass {name} once: `{{command}}`"),
                positions.splice(at, len, &[]),
                &words,
                "Pass the option once; `{command}` shows the syntax",
            )
        };
        let no_value = || {
            argv_or_help(
                format!("Pass {name} without a value: `{{command}}`"),
                positions.splice(at, 1, &[name]),
                &words,
                "Pass the flag without a value; `{command}` shows the syntax",
            )
        };
        match name {
            "--json" | "--yes" | "--preview" => {
                if inline.is_some() {
                    return Err(reject(
                        format!("option {name} does not take a value"),
                        no_value(),
                    ));
                }
                let slot = match name {
                    "--json" => &mut invocation.json,
                    "--yes" => &mut invocation.yes,
                    _ => &mut invocation.preview,
                };
                if *slot {
                    return Err(reject(
                        format!("option {name} was specified more than once"),
                        once(1),
                    ));
                }
                *slot = true;
                match name {
                    "--json" => json_at = Some(at),
                    "--preview" => preview_at = Some(at),
                    _ => {}
                }
            }
            // The global --no-color is accepted after the command path too.
            "--no-color" => {
                if inline.is_some() {
                    return Err(reject(
                        format!("option {name} does not take a value"),
                        no_value(),
                    ));
                }
            }
            "--output" => {
                let value = take_value(&mut index)?;
                let Ok(mode) = OutputMode::parse(&value) else {
                    return Err(reject(
                        format!(
                            "invalid output mode {value_quoted}; expected human, json, or jsonl",
                            value_quoted = crate::error::quote(&value)
                        ),
                        Correction::Text(
                            "Use --output human, --output json or --output jsonl.".to_owned(),
                        ),
                    ));
                };
                if invocation.output.is_some() {
                    return Err(reject(
                        "option --output was specified more than once",
                        once(width),
                    ));
                }
                invocation.output = Some(mode);
            }
            _ => {
                let Some(option) = known(name) else {
                    let mut names = options
                        .iter()
                        .filter_map(|option| option["name"].as_str())
                        .collect::<Vec<_>>();
                    names.extend(["--json", "--yes", "--preview", "--help", "--output"]);
                    if let Some(rejection) = conversion_rejection(
                        name,
                        inline,
                        rest.get(index),
                        at,
                        &names,
                        &command,
                        &words,
                        positions,
                    ) {
                        return Err(rejection);
                    }
                    if let Some(rejection) =
                        spec_key_rejection(operation, name, rest, &command, positions)
                    {
                        return Err(rejection);
                    }
                    if let Some(rejection) = argument_option_rejection(
                        name, inline, at, rest, &arguments, &options, &command, &words, positions,
                    ) {
                        return Err(rejection);
                    }
                    if let Some(rejection) =
                        schedule_rejection(operation, name, rest, &command, positions)
                    {
                        return Err(rejection);
                    }
                    // An option dropped with a reason (`--quiet`, `--jq FILTER`).
                    let id = operation["id"].as_str().unwrap_or_default();
                    if let Some((why, takes_value)) = option_removal(name, id) {
                        let valued = takes_value
                            && inline.is_none()
                            && rest.get(index).is_some_and(|value| !value.starts_with('-'));
                        let reason = format!("unknown option {name} for `{command}`; {why}");
                        let width = 1 + usize::from(valued);
                        if id == "run.share" {
                            return Err(reject(
                                reason,
                                share_correction(at, width, rest, &words, positions),
                            ));
                        }
                        return Err(reject(
                            reason,
                            argv_or_help(
                                format!("Drop {name}: `{{command}}`"),
                                positions.splice(at, width, &[]),
                                &words,
                                "Drop the option; `{command}` shows the options",
                            ),
                        ));
                    }
                    // A KEY=VALUE option (`--input`) is the alias only for a
                    // value that has `=` (`--env K=V`, but `--env linux`).
                    let given = inline.map(str::to_owned).or_else(|| {
                        rest.get(index)
                            .filter(|value| !value.starts_with('-'))
                            .cloned()
                    });
                    let fits = |candidate: &str| {
                        known(candidate)
                            .and_then(|option| option["value"].as_str())
                            .is_none_or(|value| {
                                !value.starts_with("KEY=")
                                    || given.as_deref().is_some_and(|given| given.contains('='))
                            })
                    };
                    let valued_option = |candidate: &str| {
                        candidate == "--output"
                            || known(candidate).is_some_and(|option| !option["value"].is_null())
                    };
                    // The word after the option is its value when it is
                    // inline, or when the command has no positional slot
                    // left for it.
                    let capacity = if arguments
                        .iter()
                        .any(|argument| argument["variadic"] == true)
                    {
                        usize::MAX
                    } else {
                        arguments.len()
                    };
                    let rest_words = rest.iter().map(String::as_str).collect::<Vec<_>>();
                    let has_value = inline.is_some()
                        || (given.is_some()
                            && positional_count(Some(operation), &rest_words) > capacity);
                    let aliased = option_alias(name, &names, fits);
                    if aliased == Some("--before") {
                        return Err(paging_rejection(
                            name,
                            inline,
                            given.as_deref(),
                            at,
                            &command,
                            &words,
                            positions,
                        ));
                    }
                    if let Some(rejection) = aliased.and_then(|target| {
                        duration_rejection(
                            operation,
                            name,
                            target,
                            inline,
                            given.as_deref(),
                            at,
                            &command,
                            &words,
                            positions,
                        )
                    }) {
                        return Err(rejection);
                    }
                    let found = suggest_option(name, &names, fits, has_value, valued_option);
                    let hint = found
                        .map(|found| format!("; did you mean {found}?"))
                        .unwrap_or_default();
                    let correction = match found {
                        Some(found) => {
                            let replacement = inline.map_or_else(
                                || found.to_owned(),
                                |value| format!("{found}={value}"),
                            );
                            argv_or_help(
                                format!("Replace {name} with {found}: `{{command}}`"),
                                positions.splice(at, 1, &[replacement.as_str()]),
                                &words,
                                "Use one of the listed options; `{command}` shows them",
                            )
                        }
                        // No mapping: the suggestion is the argv without it.
                        None => argv_or_help(
                            format!(
                                "Remove {name}, which `{command}` does not take: `{{command}}`"
                            ),
                            positions.splice(at, 1, &[]),
                            &words,
                            "Remove the option; `{command}` shows the options",
                        ),
                    };
                    return Err(reject(
                        format!("unknown option {name} for `{command}`{hint}"),
                        correction,
                    ));
                };
                if option["value"].is_null() {
                    if inline.is_some() {
                        return Err(reject(
                            format!("option {name} does not take a value"),
                            no_value(),
                        ));
                    }
                    if !invocation.flags.insert(name.to_owned()) {
                        return Err(reject(
                            format!("option {name} was specified more than once"),
                            once(1),
                        ));
                    }
                } else {
                    let value = take_value(&mut index)?;
                    // An option with `choices` accepts only those values.
                    if let Some(choices) = option["choices"].as_array() {
                        let choices = choices.iter().filter_map(Value::as_str).collect::<Vec<_>>();
                        if !choices.contains(&value.as_str()) {
                            let lower = value.to_ascii_lowercase();
                            let near = choices
                                .iter()
                                .copied()
                                .find(|choice| *choice == lower)
                                .or_else(|| nearest(&lower, &choices, 1).first().copied())
                                .unwrap_or_default();
                            let fixed = if inline.is_some() {
                                positions.splice(at, 1, &[&format!("{name}={near}")])
                            } else {
                                positions.splice(at, 2, &[name, near])
                            };
                            return Err(reject(
                                format!(
                                    "option {name} must be one of {}; got {value_quoted}",
                                    choices.join(", "),
                                    value_quoted = crate::error::quote(&value)
                                ),
                                argv_or_help(
                                    format!("Use {name} {near}: `{{command}}`"),
                                    fixed,
                                    &words,
                                    "Use one of the listed values; `{command}` shows them",
                                ),
                            ));
                        }
                    }
                    let values = invocation.options.entry(name.to_owned()).or_default();
                    if !values.is_empty() && option["repeatable"] != true {
                        return Err(reject(
                            format!("option {name} was specified more than once"),
                            once(width),
                        ));
                    }
                    values.push(value);
                }
            }
        }
    }
    if invocation.preview && operation["preview"] != true {
        return Err(reject(
            format!("`{command}` does not change anything, so --preview does not apply"),
            argv_or_help(
                format!("Drop --preview; `{command}` only reads: `{{command}}`"),
                preview_at.and_then(|at| positions.splice(at, 1, &[])),
                &words,
                "Drop --preview; `{command}` shows the syntax",
            ),
        ));
    }
    if invocation.json
        && invocation
            .output
            .is_some_and(|mode| mode != OutputMode::Json)
    {
        return Err(reject(
            "--json conflicts with --output",
            argv_or_help(
                "Keep one output choice; without --json: `{command}`".to_owned(),
                json_at.and_then(|at| positions.splice(at, 1, &[])),
                &words,
                "Keep one of --json and --output; `{command}` shows the syntax",
            ),
        ));
    }
    // `cli program save FILE`: the one argument is the program file, so the
    // missing argument is the slug that comes first.
    if operation["id"] == "program.save" && positionals.len() == 1 {
        let (at, file) = &positionals[0];
        if file.ends_with(".md") || file.contains('/') {
            let slug = slug_from_file(file);
            return Err(reject(
                format!(
                    "missing argument <SLUG> for `{command}`; {file_quoted} is the program file, which comes after the slug: `{command} <SLUG> <FILE>`",
                    file_quoted = crate::error::quote(file)
                ),
                argv_or_help(
                    "Pass the slug before the file: `{command}`".to_owned(),
                    slug.and_then(|slug| positions.splice(*at, 1, &[&slug, file])),
                    &words,
                    "Pass <SLUG> before <FILE>; `{command}` shows the arguments",
                ),
            ));
        }
    }
    // `cli program save FILE SLUG`: the arguments are swapped.
    if operation["id"] == "program.save" && positionals.len() == 2 {
        let looks_like_file = |value: &str| value.ends_with(".md") || value.contains('/');
        let (first_at, first) = &positionals[0];
        let (second_at, second) = &positionals[1];
        if looks_like_file(first) && !looks_like_file(second) && program_ref::valid_slug(second) {
            let swapped = positions.splice(*first_at, 1, &[second]).and_then(|argv| {
                let offset = positions.offset? + second_at;
                let mut argv = argv;
                first.clone_into(argv.get_mut(offset)?);
                Some(argv)
            });
            return Err(reject(
                format!(
                    "the arguments of `{command}` are swapped: {first_quoted} is the program file, which comes after the slug {second_quoted}: `{command} <SLUG> <FILE>`",
                    first_quoted = crate::error::quote(first),
                    second_quoted = crate::error::quote(second)
                ),
                argv_or_help(
                    "Pass the slug before the file: `{command}`".to_owned(),
                    swapped,
                    &words,
                    "Pass <SLUG> before <FILE>; `{command}` shows the arguments",
                ),
            ));
        }
    }
    let mut values = positionals.into_iter();
    for argument in &arguments {
        let name = argument["name"].as_str().unwrap_or_default().to_owned();
        let missing = |invocation: &ServiceInvocation| {
            let correction = match lister(&words, &name, invocation) {
                Some((what, list)) => Correction::Command {
                    action: format!("Pass <{name}>; list {what} with `{{command}}`"),
                    words: list,
                },
                None => Correction::Command {
                    action: format!(
                        "Pass <{name}>; `{{command}}` shows the arguments of `{command}`"
                    ),
                    words: help_words(&words),
                },
            };
            reject(
                format!("missing argument <{name}> for `{command}`"),
                correction,
            )
        };
        if argument["variadic"] == true {
            let collected = values.by_ref().map(|(_, value)| value).collect::<Vec<_>>();
            if collected.is_empty() && argument["required"] == true {
                return Err(missing(invocation));
            }
            if !collected.is_empty() {
                invocation.arguments.insert(name, collected);
            }
            continue;
        }
        match values.next() {
            Some((_, value)) => {
                invocation.arguments.insert(name, vec![value]);
            }
            None if argument["required"] == true => {
                return Err(missing(invocation));
            }
            None => {}
        }
    }
    if let Some((at, extra)) = values.next() {
        // `cli run share RUN_ID someone@example.com`: a link is never shared
        // with one person.
        if operation["id"] == "run.share" && extra.contains('@') {
            return Err(reject(
                format!(
                    "unexpected argument {extra:?} for `{command}`; `{command}` mints a public, unrevocable 24-hour link that anyone who has it can open; it is not shared with one person"
                ),
                share_correction(at, 1, rest, &words, positions),
            ));
        }
        if let Some(rejection) = extra_argument_rejection(
            &options, invocation, at, &extra, &command, &words, positions,
        ) {
            return Err(rejection);
        }
        return Err(reject(
            format!(
                "unexpected argument {extra_quoted} for `{command}`",
                extra_quoted = crate::error::quote(&extra)
            ),
            argv_or_help(
                format!(
                    "Remove the extra argument {extra_quoted}: `{{command}}`",
                    extra_quoted = crate::error::quote(&extra)
                ),
                positions.splice(at, 1, &[]),
                &words,
                "Remove the extra argument; `{command}` shows the arguments",
            ),
        ));
    }
    for option in &options {
        let name = option["name"].as_str().unwrap_or_default();
        if option["required"] == true && !invocation.options.contains_key(name) {
            let value = option["value"].as_str().unwrap_or("VALUE");
            return Err(reject(
                format!("missing required option {name} for `{command}`"),
                Correction::Command {
                    action: format!(
                        "Add {name} {value}; `{{command}}` shows the options of `{command}`"
                    ),
                    words: help_words(&words),
                },
            ));
        }
    }
    Ok(())
}

/// The argument an option spelling names (`--slug` names `SLUG`, `--run-id`
/// names `RUN_ID`, `--slug` also names the SLUG of `OWNER/SLUG[@REV]`).
fn named_argument<'a>(name: &str, arguments: &'a [Value]) -> Option<(usize, &'a str)> {
    let wanted = name
        .strip_prefix("--")?
        .to_ascii_uppercase()
        .replace('-', "_");
    arguments.iter().enumerate().find_map(|(index, argument)| {
        let argument_name = argument["name"].as_str()?;
        let parts = argument_name
            .split(|character: char| !(character.is_ascii_uppercase() || character == '_'))
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>();
        (argument_name == wanted || (wanted == "SLUG" && parts.contains(&"SLUG")))
            .then_some((index, argument_name))
    })
}

/// An option spelling of a positional argument (`cli org create --slug
/// acme --name Acme`): the suggestion passes the value as that argument, in
/// its place among the other arguments (identical in both ports).
#[allow(clippy::too_many_arguments)]
fn argument_option_rejection(
    name: &str,
    inline: Option<&str>,
    at: usize,
    rest: &[String],
    arguments: &[Value],
    options: &[Value],
    command: &str,
    words: &[&str],
    positions: &Positions<'_>,
) -> Option<Rejection> {
    let (slot, argument) = named_argument(name, arguments)?;
    let (value, width) = match inline {
        Some(value) => (value.to_owned(), 1),
        None => {
            let value = rest.get(at + 1).filter(|value| !value.starts_with('-'))?;
            (value.clone(), 2)
        }
    };
    // The other tokens, and which of them are positional arguments.
    let takes_value = |token: &str| {
        !token.contains('=')
            && (token == "--output"
                || options
                    .iter()
                    .any(|option| option["name"] == token && !option["value"].is_null()))
    };
    let mut kept = Vec::new();
    let mut positional = Vec::new();
    let mut index = 0;
    let mut literal = false;
    while let Some(token) = rest.get(index) {
        if index == at {
            index += width;
            continue;
        }
        if literal || token == "-" || !token.starts_with('-') {
            positional.push(kept.len());
        } else if token == "--" {
            literal = true;
        } else if takes_value(token) {
            kept.push(token.clone());
            index += 1;
            if let Some(value) = rest.get(index) {
                kept.push(value.clone());
                index += 1;
            }
            continue;
        }
        kept.push(token.clone());
        index += 1;
    }
    let insert_at = positional
        .get(slot)
        .copied()
        .or_else(|| positional.last().map(|last| last + 1))
        .unwrap_or(0);
    kept.insert(insert_at, value);
    let with = kept.iter().map(String::as_str).collect::<Vec<_>>();
    Some(reject(
        format!(
            "unknown option {name} for `{command}`; <{argument}> is an argument, not an option"
        ),
        argv_or_help(
            format!("Pass <{argument}> as an argument: `{{command}}`"),
            positions.splice(0, rest.len(), &with),
            words,
            "Pass the argument without an option name; `{command}` shows the arguments",
        ),
    ))
}

/// `--amount 5` on an operation with `--amount-cents` (`optionConversions`): the suggestion converts a positive whole number and says
/// so; any other value is not converted, and the reason states the unit.
#[allow(clippy::too_many_arguments)]
fn conversion_rejection(
    name: &str,
    inline: Option<&str>,
    next: Option<&String>,
    at: usize,
    names: &[&str],
    command: &str,
    words: &[&str],
    positions: &Positions<'_>,
) -> Option<Rejection> {
    let (target, multiplier, from, to) = option_conversion(name, names)?;
    let value = inline.or_else(|| {
        next.map(String::as_str)
            .filter(|value| !value.starts_with('-'))
    });
    let converted = value.and_then(|value| converted_value(value, multiplier));
    Some(match (value, converted) {
        (Some(value), Some(converted)) => {
            let width = if inline.is_some() { 1 } else { 2 };
            let replacement = if inline.is_some() {
                vec![format!("{target}={converted}")]
            } else {
                vec![target.to_owned(), converted.clone()]
            };
            let replacement = replacement.iter().map(String::as_str).collect::<Vec<_>>();
            reject(
                format!(
                    "unknown option {name} for `{command}`; {target} counts {to}, so {name} {value} ({from}) is {target} {converted}"
                ),
                argv_or_help(
                    format!("Use {target} {converted} ({from} converted to {to}): `{{command}}`"),
                    positions.splice(at, width, &replacement),
                    words,
                    "Use the listed option; `{command}` shows it",
                ),
            )
        }
        (value, _) => {
            let given = value.map_or_else(String::new, |value| {
                format!(
                    "; {value_quoted} is not a positive whole number of {from}, so it was not converted", value_quoted = crate::error::quote(value))
            });
            reject(
                format!(
                    "unknown option {name} for `{command}`; the option is {target}, a whole number of {to} ({target} {} for 5 {from}){given}",
                    5 * multiplier
                ),
                Correction::Command {
                    action: format!(
                        "Pass {target} with a whole number of {to}; `{{command}}` shows the options of `{command}`"
                    ),
                    words: help_words(words),
                },
            )
        }
    })
}

/// The Action of a `cli run share` correction: the link is public, never
/// for one person.
const SHARE_ACTION: &str = "`cli run share` makes a public, unrevocable 24-hour link, and `cli org invite` gives one person access; preview the link first: `{command}`";

/// A `cli run share` argv that tried to name a person: `width` tokens at
/// `at` are dropped, and so is `--yes`, and `--preview` is added, so the
/// suggestion never mints a public link on its own.
fn share_correction(
    at: usize,
    width: usize,
    rest: &[String],
    words: &[&str],
    positions: &Positions<'_>,
) -> Correction {
    let (Some(base), Some(offset)) = (positions.splice(at, width, &[]), positions.offset) else {
        return Correction::Command {
            action: SHARE_ACTION.replace(": `{command}`", "; `{command}` shows the syntax"),
            words: help_words(words),
        };
    };
    let end = offset + rest.len() - width;
    let region = &base[offset..end];
    let stop = region
        .iter()
        .position(|token| token == "--")
        .unwrap_or(region.len());
    let mut head = region[..stop]
        .iter()
        .filter(|token| *token != "--yes")
        .cloned()
        .collect::<Vec<_>>();
    if !head.iter().any(|token| token == "--preview") {
        head.push("--preview".to_owned());
    }
    let mut argv = base[..offset].to_vec();
    argv.extend(head);
    argv.extend_from_slice(&region[stop..]);
    argv.extend_from_slice(&base[end..]);
    Correction::Argv {
        action: SHARE_ACTION.to_owned(),
        argv,
    }
}

/// `--page 2`, `--cursor C` or `--offset 20` on a paged listing: pages are
/// cursors, so a number (or no value) is dropped and the Action names
/// result.nextBefore; any other value is the cursor --before takes.
fn paging_rejection(
    name: &str,
    inline: Option<&str>,
    given: Option<&str>,
    at: usize,
    command: &str,
    words: &[&str],
    positions: &Positions<'_>,
) -> Rejection {
    match given {
        Some(value) if !value.bytes().all(|byte| byte.is_ascii_digit()) => {
            let replacement = inline.map_or_else(
                || "--before".to_owned(),
                |value| format!("--before={value}"),
            );
            reject(
                format!(
                    "unknown option {name} for `{command}`; did you mean --before? It takes the cursor a previous page returned in result.nextBefore"
                ),
                argv_or_help(
                    format!(
                        "Replace {name} with --before, which takes the previous page's result.nextBefore: `{{command}}`"
                    ),
                    positions.splice(at, 1, &[replacement.as_str()]),
                    words,
                    "Use one of the listed options; `{command}` shows them",
                ),
            )
        }
        _ => {
            let width = if inline.is_none() && given.is_some() {
                2
            } else {
                1
            };
            reject(
                format!(
                    "unknown option {name} for `{command}`; pages are cursors, not numbers: --before takes the result.nextBefore of the previous page"
                ),
                argv_or_help(
                    "List the first page with `{command}`, then pass its result.nextBefore to --before for the next page".to_owned(),
                    positions.splice(at, width, &[]),
                    words,
                    "Pass a previous result's result.nextBefore to --before; `{command}` shows the options",
                ),
            )
        }
    }
}

/// `--timeout 30` for a DURATION option: the alias target with the unit a
/// bare positive whole number lacks (`--wait 30s`). `None` for any other
/// value or target.
#[allow(clippy::too_many_arguments)]
fn duration_rejection(
    operation: &Value,
    name: &str,
    target: &str,
    inline: Option<&str>,
    given: Option<&str>,
    at: usize,
    command: &str,
    words: &[&str],
    positions: &Positions<'_>,
) -> Option<Rejection> {
    let spec = operation["options"]
        .as_array()?
        .iter()
        .find(|option| option["name"] == target)?["value"]
        .as_str()?;
    let given = given?;
    if spec != "DURATION"
        || given.is_empty()
        || given.len() > 9
        || !given.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let number = given.parse::<u64>().ok().filter(|number| *number > 0)?;
    let value = format!("{number}s");
    let joined = format!("{target}={value}");
    let replacement: Vec<&str> = if inline.is_some() {
        vec![joined.as_str()]
    } else {
        vec![target, value.as_str()]
    };
    Some(reject(
        format!(
            "unknown option {name} for `{command}`; did you mean {target}? {target} takes a duration with a unit, so {given} is {value}"
        ),
        argv_or_help(
            format!("Use {target} {value}: `{{command}}`"),
            positions.splice(at, if inline.is_some() { 1 } else { 2 }, &replacement),
            words,
            "Use the listed option; `{command}` shows it",
        ),
    ))
}

/// Seconds in an interval value: a positive whole number, optionally with
/// s, m, h or d.
fn interval_seconds(value: &str) -> Option<u64> {
    let (digits, unit) = match value.char_indices().last()? {
        (index, 's' | 'm' | 'h' | 'd') => (&value[..index], &value[index..]),
        _ => (value, ""),
    };
    if digits.is_empty() || digits.len() > 9 || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let factor = match unit {
        "m" => 60,
        "h" => 3600,
        "d" => 86400,
        _ => 1,
    };
    digits
        .parse::<u64>()
        .ok()
        .map(|number| number * factor)
        .filter(|seconds| *seconds > 0)
}

/// `cli job create --cron ...`, `--every 1h`, `--daily` or `--program SLUG`:
/// a job is a JSON spec. The suggestion reads a schedule spec from standard
/// input with --preview; `details.suggestedSpec` is the object and
/// `details.suggestedStdin` the line to pipe. A given program becomes
/// `program_ref`, a given interval `interval_seconds` (default 86400, daily).
fn schedule_rejection(
    operation: &Value,
    name: &str,
    rest: &[String],
    command: &str,
    positions: &Positions<'_>,
) -> Option<Rejection> {
    let id = operation["id"].as_str()?;
    if id != "job.create" {
        return None;
    }
    let schedule_option = |flag: &str| -> Option<(&'static str, bool)> {
        let key = if flag == "--from" { "--program" } else { flag };
        let entry = &inference()["optionRemovals"][key];
        entry["operations"]
            .as_array()?
            .iter()
            .any(|operation| operation == id)
            .then(|| {
                entry["why"]
                    .as_str()
                    .map(|why| (why, entry["value"] == true))
            })
            .flatten()
    };
    let (why, _) = schedule_option(name)?;
    let mut program: Option<String> = None;
    let mut interval: Option<u64> = None;
    let mut kept: Vec<String> = Vec::new();
    let mut index = 0;
    while let Some(token) = rest.get(index) {
        if token == "--" {
            break;
        }
        let (flag, inline) = match token.split_once('=') {
            Some((flag, value)) if flag.starts_with("--") => (flag, Some(value.to_owned())),
            _ => (token.as_str(), None),
        };
        let entry = if token.starts_with('-') {
            schedule_option(flag)
        } else {
            None
        };
        if let Some((_, takes_value)) = entry {
            let mut value = inline;
            let mut width = 1;
            if value.is_none() && takes_value {
                if let Some(next) = rest.get(index + 1).filter(|next| !next.starts_with('-')) {
                    value = Some(next.clone());
                    width = 2;
                }
            }
            if let Some(value) = &value {
                if matches!(flag, "--program" | "--from") {
                    program = Some(value.clone());
                }
                if matches!(flag, "--every" | "--interval") {
                    interval = interval_seconds(value).or(interval);
                }
            }
            if flag == "--daily" {
                interval = Some(86400);
            }
            index += width;
            continue;
        }
        if matches!(flag, "--json" | "--no-color") {
            kept.push(token.clone());
        }
        if flag == "--output" {
            kept.push(token.clone());
            if inline.is_none() {
                if let Some(value) = rest.get(index + 1) {
                    kept.push(value.clone());
                    index += 1;
                }
            }
        }
        index += 1;
    }
    let spec = json!({
        "interval_seconds": interval.unwrap_or(86400),
        "program_ref": program.as_deref().unwrap_or("your-program"),
        "type": "schedule",
    });
    let text = serde_json::to_string(&spec).ok()?;
    let mut spec_argv = kept.iter().map(String::as_str).collect::<Vec<_>>();
    spec_argv.extend(["--spec-file", "-", "--preview"]);
    let argv = positions.splice(0, rest.len(), &spec_argv);
    let replace = if program.is_none() {
        " (replace your-program with your program's slug; `cli program list` lists them)"
    } else {
        ""
    };
    let correction = match argv {
        Some(argv) => Correction::Argv {
            action: format!(
                "Create a schedule job from a JSON spec on standard input{replace}, previewing it first: `printf '%s\\n' {} | {{command}}`",
                render::shell_quote(&text)
            ),
            argv,
        },
        None => Correction::Command {
            action: "Pass a schedule spec with --spec-file; `{command}` shows the spec".to_owned(),
            words: help_words(&command_words(operation)),
        },
    };
    let mut rejection = reject(
        format!("unknown option {name} for `{command}`; {why}"),
        correction,
    );
    rejection.error = rejection
        .error
        .with_detail("suggestedSpec", spec)
        .with_detail("suggestedStdin", Value::String(format!("{text}\n")));
    Some(rejection)
}

/// `cli job update ID --name x`: an unknown option that names
/// a key of the operation's --spec-file object. The suggestion reads the
/// spec from standard input, and `details.suggestedStdin` holds the JSON
/// object of every such option given. Secret keys (`*_secret`), object keys
/// and an argv that already has the spec option keep the ordinary rejection.
fn spec_key_rejection(
    operation: &Value,
    name: &str,
    rest: &[String],
    command: &str,
    positions: &Positions<'_>,
) -> Option<Rejection> {
    let spec = &operation["spec"];
    let option = spec["option"].as_str()?;
    let keys = spec["keys"].as_object()?;
    let key_of = |token: &str| -> Option<(String, &Value)> {
        let key = token.strip_prefix("--")?.replace('-', "_");
        let kind = keys.get(&key)?;
        let simple = kind
            .as_str()
            .is_some_and(|kind| matches!(kind, "text" | "interval"))
            || kind.get("enum").is_some();
        (simple && !key.ends_with("_secret")).then_some((key, kind))
    };
    key_of(name)?;
    let mut object = Map::new();
    let mut spans: Vec<(usize, usize)> = Vec::new();
    let mut index = 0;
    while let Some(token) = rest.get(index) {
        if token == "--" {
            break;
        }
        if token == option || token.starts_with(&format!("{option}=")) {
            return None;
        }
        let (flag, inline) = match token.split_once('=') {
            Some((flag, value)) if flag.starts_with("--") => (flag, Some(value)),
            _ => (token.as_str(), None),
        };
        if let Some((key, kind)) = key_of(flag) {
            let value = match inline {
                Some(value) => value.to_owned(),
                None => rest.get(index + 1)?.clone(),
            };
            let value = match kind.as_str() {
                // A JSON number both ports read exactly (at most 2^53 - 1).
                Some("interval") => value
                    .parse::<u64>()
                    .ok()
                    .filter(|number| *number <= 9_007_199_254_740_991)
                    .map_or_else(|| Value::String(value.clone()), Value::from),
                _ => Value::String(value),
            };
            object.insert(key, value);
            let width = if inline.is_some() { 1 } else { 2 };
            spans.push((index, width));
            index += width;
            continue;
        }
        index += 1;
    }
    let first = spans.first()?.0;
    let mut argv = positions.splice(0, 0, &[])?;
    let offset = argv.len() - rest.len();
    for &(start, width) in spans.iter().rev() {
        let range = offset + start..offset + start + width;
        if start == first {
            argv.splice(range, [option.to_owned(), "-".to_owned()]);
        } else {
            argv.drain(range);
        }
    }
    let json = serde_json::to_string(&Value::Object(object)).ok()?;
    let listed = spans
        .iter()
        .map(|(start, _)| {
            rest[*start]
                .split_once('=')
                .map_or(rest[*start].as_str(), |(flag, _)| flag)
                .to_owned()
        })
        .collect::<Vec<_>>()
        .join(", ");
    let what = if spans.len() == 1 {
        "is a key of the"
    } else {
        "are keys of the"
    };
    let mut rejection = reject(
        format!(
            "unknown option {name} for `{command}`; {listed} {what} {option} JSON object: {json}"
        ),
        Correction::Argv {
            action: format!(
                "Pass the settings as JSON on standard input: `printf '%s\\n' {} | {{command}}`",
                render::shell_quote(&json)
            ),
            argv,
        },
    );
    rejection.error = rejection
        .error
        .with_detail("suggestedStdin", Value::String(format!("{json}\n")));
    Some(rejection)
}

/// An unexpected positional argument with a known meaning.
/// On an operation that reads a secret from a `secretFileOptions` option
/// (`cli wallet redeem CODE`) the argument is presumed to be the secret: it
/// is never echoed, and the suggestion reads the option from standard
/// input. Otherwise, when exactly one required value option is missing, the
/// argument is its value (`cli wallet topup 500` -> `--amount-cents 500`),
/// unless the option names a file that does not exist or a number and the
/// argument is not a whole number.
fn extra_argument_rejection(
    options: &[Value],
    invocation: &ServiceInvocation,
    at: usize,
    extra: &str,
    command: &str,
    words: &[&str],
    positions: &Positions<'_>,
) -> Option<Rejection> {
    let secret = options.iter().find_map(|option| {
        let name = option["name"].as_str()?;
        secret_placeholder(name).map(|placeholder| (name, placeholder))
    });
    if let Some((name, placeholder)) = secret {
        let given = invocation.options.contains_key(name);
        let reason = format!(
            "`{command}` never takes the {} as an argument, so the argument was not echoed; it reads {name}, a file or - for standard input",
            placeholder.to_lowercase()
        );
        return Some(if given {
            reject(
                reason,
                argv_or_help(
                    "Remove the extra argument: `{command}`".to_owned(),
                    positions.splice(at, 1, &[]),
                    words,
                    "Remove the extra argument; `{command}` shows the arguments",
                ),
            )
        } else {
            reject(
                reason,
                argv_or_help(
                    format!(
                        "Pipe the {} in on standard input: `printf '%s\\n' \"${placeholder}\" | {{command}}`",
                        placeholder.to_lowercase()
                    ),
                    positions.splice(at, 1, &[name, "-"]),
                    words,
                    "Pass the secret with its file option; `{command}` shows it",
                ),
            )
        });
    }
    let missing = options
        .iter()
        .filter(|option| option["required"] == true && !option["value"].is_null())
        .filter(|option| {
            option["name"]
                .as_str()
                .is_some_and(|name| !invocation.options.contains_key(name))
        })
        .collect::<Vec<_>>();
    let [option] = missing.as_slice() else {
        return None;
    };
    let name = option["name"].as_str()?;
    let fits = match option["value"].as_str() {
        Some("N") => !extra.is_empty() && extra.bytes().all(|byte| byte.is_ascii_digit()),
        Some("FILE") => extra == "-" || positions_cwd_file(extra),
        _ => true,
    };
    if !fits || (extra.starts_with('-') && extra != "-") {
        return None;
    }
    let description = option["description"].as_str().unwrap_or_default();
    Some(reject(
        format!(
            "unexpected argument {extra_quoted} for `{command}`; did you mean {name} {extra}? {name}: {description}",
            extra_quoted = crate::error::quote(extra)
        ),
        argv_or_help(
            format!("Pass it as {name}: `{{command}}`"),
            positions.splice(at, 1, &[name, extra]),
            words,
            "Pass the value with its option; `{command}` shows the options",
        ),
    ))
}

/// Whether `path` names an existing file, relative to the working directory.
fn positions_cwd_file(path: &str) -> bool {
    std::path::Path::new(path).is_file()
}

/// The service origin, the only one a public build knows.
pub const PRODUCTION_ORIGIN: &str = "https://run-prose-production.openprose.workers.dev";

/// The environment variable that holds the API key.
pub const CREDENTIAL_ENV: &str = "OPENPROSE_API_KEY";

/// The OS credential-store service of the production key. It predates the
/// custom endpoint, so existing logins keep working.
pub const PRODUCTION_STORE_SERVICE: &str = "org.openprose.cli.production";

/// The resolved service. A public build always resolves
/// production; only a `dev-endpoint` build can resolve a custom origin
/// (`OPENPROSE_API_URL`), whose key lives in its own credential-store entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Environment {
    /// `production`, or `custom` in a `dev-endpoint` build. The JSON
    /// `environment` field.
    pub name: &'static str,
    pub origin: String,
    pub credential_env: &'static str,
    /// The OS credential-store service name (account `api-key`). A closed
    /// ASCII alphabet: it is interpolated into the macOS `security -i` script.
    pub store_service: String,
}

impl Environment {
    /// The production service.
    #[must_use]
    pub fn production() -> Self {
        Self {
            name: "production",
            origin: PRODUCTION_ORIGIN.to_owned(),
            credential_env: CREDENTIAL_ENV,
            store_service: PRODUCTION_STORE_SERVICE.to_owned(),
        }
    }

    /// The service this build talks to. A public build ignores the process
    /// environment and returns production.
    ///
    /// # Errors
    /// Only in a `dev-endpoint` build: `CONFIG_INVALID` when the endpoint
    /// override is set but is not an https origin.
    pub fn resolve(environment: &BTreeMap<String, String>) -> Result<Self, RunnerError> {
        #[cfg(feature = "dev-endpoint")]
        if let Some(custom) = dev_endpoint::custom(environment)? {
            return Ok(custom);
        }
        let _ = environment;
        Ok(Self::production())
    }

    /// Human label: `OpenProse`, or `OpenProse (custom endpoint <origin>)`.
    pub fn label(&self) -> String {
        #[cfg(feature = "dev-endpoint")]
        if self.is_custom() {
            return dev_endpoint::label(&self.origin);
        }
        "OpenProse".to_owned()
    }

    /// Whether this is a `dev-endpoint` build's custom origin.
    pub fn is_custom(&self) -> bool {
        self.name != "production"
    }

    /// Journal directory component: `production`, or `custom-<digest>` for a
    /// custom origin, so journals of different services never mix.
    pub fn journal_component(&self) -> String {
        #[cfg(feature = "dev-endpoint")]
        if self.is_custom() {
            return dev_endpoint::scope(&self.origin);
        }
        self.name.to_owned()
    }
}

/// The strict credential predicate (`^rr_test_[0-9a-f]{32}$`).
pub fn valid_credential(token: &str) -> bool {
    token.strip_prefix("rr_test_").is_some_and(|value| {
        value.len() == 32
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

/// Result of the confirmation gate.
#[derive(Debug)]
pub enum Gate {
    /// Send the request.
    Proceed,
    /// `--preview`: return this result without sending.
    Preview(Value),
}

/// Execution context handed to feature handlers.
pub struct Context<'a> {
    pub invocation: &'a ServiceInvocation,
    pub operation: &'static Value,
    pub environment: Environment,
    pub mode: OutputMode,
    pub transport: http::Transport,
    pub system: &'a SystemContext,
    pub cancellation: CancellationToken,
    /// Streamed output (JSONL events, human text chunks).
    pub out: &'a mut dyn Write,
    /// Streamed diagnostics (human status and warnings).
    pub err: &'a mut dyn Write,
    /// Set by paged operations: the envelope's `nextBefore`.
    pub next_before: Option<String>,
    /// Set by stream operations: the terminal line's `runId`.
    pub run_id: Option<String>,
    /// Set by a handler that rendered its own human output.
    pub human: Option<String>,
    /// The OWNER of an own-scope `OWNER/SLUG` confirmed as the
    /// caller; the plan carries it as `plannedRequest.owner`.
    pub verified_owner: Option<String>,
    credential: Option<String>,
    /// Where the credential came from: `environment` or `store`.
    credential_source: Option<&'static str>,
    /// Set when the service rejected the key itself (401, or 403 `Invalid API key.`).
    key_rejected: std::cell::Cell<bool>,
}

impl std::fmt::Debug for Context<'_> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Context")
            .field("operation", &self.invocation.operation)
            .field("environment", &self.environment)
            .field("mode", &self.mode)
            .finish_non_exhaustive()
    }
}

impl Context<'_> {
    pub fn argument(&self, name: &str) -> Option<&str> {
        self.invocation.argument(name)
    }

    /// A copyable follow-up command line (`words` follow `cli`) that keeps
    /// this invocation's machine output mode.
    pub fn command(&self, words: &str) -> String {
        render::follow_up_command(self.mode, words)
    }

    /// Rewrites every `` `prose cli ...` `` command in a fixed hint so it keeps
    /// this invocation's machine output mode.
    pub fn localize(&self, text: &str) -> String {
        text.replace("`prose cli ", &format!("`{} ", self.command("")))
    }

    /// A follow-up argv (after the product name); `words` follow `cli`.
    pub fn follow_up_argv(&self, words: &[&str]) -> Vec<String> {
        render::follow_up_argv(self.mode, words)
    }

    pub fn option(&self, name: &str) -> Option<&str> {
        self.invocation.option(name)
    }

    pub fn flag(&self, name: &str) -> bool {
        self.invocation.flag(name)
    }

    /// The manifest request template at `index`.
    pub fn request(&self, index: usize) -> &'static Value {
        &self.operation["requests"][index]
    }

    /// The bearer credential: the environment variable, then the OS
    /// credential store.
    pub fn credential(&mut self) -> Result<String, RunnerError> {
        if let Some(token) = &self.credential {
            return Ok(token.clone());
        }
        let variable = self.environment.credential_env;
        let from_environment = self
            .system
            .environment
            .get(variable)
            .is_some_and(|value| !value.is_empty());
        let source = if from_environment {
            "environment"
        } else {
            "store"
        };
        let token = match self
            .system
            .environment
            .get(variable)
            .filter(|v| !v.is_empty())
        {
            Some(token) => token.clone(),
            None => self
                .transport
                .stored_credential(&self.environment)
                .map_err(|error| {
                    if error.code == ErrorCode::CredentialStoreUnavailable {
                        store_unavailable(error, &self.environment)
                    } else {
                        error
                    }
                })?
                .ok_or_else(|| {
                    credential_failure(
                        &self.environment,
                        self.mode,
                        "none",
                        "missing",
                        missing_reason(&self.environment, self.mode),
                    )
                })?,
        };
        if !valid_credential(&token) {
            return Err(credential_failure(
                &self.environment,
                self.mode,
                source,
                "malformed",
                self.localize(&malformed_reason(
                    &token,
                    variable,
                    from_environment,
                    &self.environment,
                )),
            ));
        }
        self.credential = Some(token.clone());
        self.credential_source = Some(source);
        Ok(token)
    }

    /// Names the credential source of a service authentication failure
    ///: an environment key is not replaced by `cli auth login`.
    fn annotate_auth(&self, error: &mut RunnerError) {
        if error.code != ErrorCode::ServiceAuthRequired {
            return;
        }
        let Some(source) = self.credential_source else {
            return;
        };
        let details = error.details.get_or_insert_with(|| Box::new(Map::new()));
        if details.contains_key("credentialSource") {
            return;
        }
        let variable = self.environment.credential_env;
        details.insert("credentialSource".into(), json!(source));
        details.insert("credentialVariable".into(), json!(variable));
        if self.key_rejected.get() {
            details.insert("credentialProblem".into(), json!("rejected"));
            details.insert(
                "reason".into(),
                json!(rejected_reason(&self.environment, self.mode, source)),
            );
            error.action = credential_action(&self.environment, self.mode, source);
        }
    }

    /// The credential if one was resolved, for redaction.
    pub fn known_credential(&self) -> Option<&str> {
        self.credential.as_deref()
    }

    /// Sends one request of the operation and returns a 2xx response;
    /// other statuses are classified into the service taxonomy.
    pub fn send(&mut self, request: &http::Request) -> Result<http::Response, RunnerError> {
        let response = self.send_raw(request)?;
        if (200..300).contains(&response.status) {
            Ok(response)
        } else {
            Err(self.classify(request, &response))
        }
    }

    /// Sends one request and returns any status (bodies capped by class).
    pub fn send_raw(&mut self, request: &http::Request) -> Result<http::Response, RunnerError> {
        let token = if request.bearer {
            Some(self.credential()?)
        } else {
            None
        };
        self.transport
            .send(&self.environment, request, token.as_deref())
    }

    /// Classifies a non-2xx response (body code, route override, status).
    pub fn classify(&self, request: &http::Request, response: &http::Response) -> RunnerError {
        if request.bearer
            && (response.status == 401
                || (response.status == 403
                    && serde_json::from_slice::<Value>(&response.body)
                        .is_ok_and(|body| body["error"] == "Invalid API key.")))
        {
            self.key_rejected.set(true);
        }
        let error = http::classify(
            self.operation["contract"].as_str().unwrap_or("service/1"),
            request,
            response,
            self.known_credential(),
        );
        discovery::paid_model_refusal(error, response, self.mode, self.known_credential())
    }

    /// Opens a server-sent event stream; see [`http::Transport::open_stream`].
    pub fn open_stream(
        &mut self,
        request: &http::Request,
    ) -> Result<http::StreamOpen, RunnerError> {
        let token = if request.bearer {
            Some(self.credential()?)
        } else {
            None
        };
        self.transport
            .open_stream(&self.environment, request, token.as_deref())
    }

    /// Streams one response body into `sink` under the download limits.
    pub fn download(
        &mut self,
        request: &http::Request,
        sink: &mut dyn Write,
        max_bytes: u64,
    ) -> Result<http::Downloaded, RunnerError> {
        let token = if request.bearer {
            Some(self.credential()?)
        } else {
            None
        };
        let contract = self.operation["contract"].as_str().unwrap_or("service/1");
        self.transport.download(
            &self.environment,
            request,
            token.as_deref(),
            sink,
            max_bytes,
            contract,
        )
    }

    /// The current time (the fixture's virtual clock in test-seam builds).
    pub fn now_rfc3339(&mut self) -> String {
        self.transport.now_rfc3339()
    }

    /// Monotonic milliseconds (virtual in test-seam builds with a clock).
    pub fn monotonic_ms(&mut self) -> u64 {
        self.transport.monotonic_ms()
    }

    /// A fresh `UUIDv4` (the fixture's `ids` in order in test-seam builds).
    pub fn uuid_v4(&mut self) -> Result<String, RunnerError> {
        self.transport.uuid_v4()
    }

    /// The run journal for this environment.
    pub fn journal(&self) -> journal::Journal {
        journal::Journal::for_environment(self.system, &self.environment)
    }

    /// Builds the planned request for `index` from concrete values.
    pub fn planned(
        &self,
        index: usize,
        path: &str,
        query: &[(String, String)],
        body: Option<&[u8]>,
    ) -> Value {
        let template = self.request(index);
        let mut planned = render::planned_request(self.operation, template, path, query, body);
        if let Some(owner) = &self.verified_owner {
            planned["owner"] = json!({"handle": owner, "verified": true});
        }
        planned
    }

    /// Refuses a `--model` this account may not run before any
    /// confirmation, naming the nearest offered models. `index` is the
    /// operation's `GET /models` request. Advisory: when the lookup fails the
    /// service's own check applies; only an interrupt stops here.
    pub fn check_model(&mut self, index: usize, model: &str) -> Result<(), RunnerError> {
        let request = http::Request::from_manifest(self.operation, index, "/models")
            .class(http::TransportClass::Control);
        let body = match self
            .send(&request)
            .and_then(|response| response.json_object())
        {
            Ok(body) => body,
            Err(error) if error.code == ErrorCode::Cancelled => return Err(error),
            Err(_) => return Ok(()),
        };
        let offered = body
            .get("models")
            .and_then(Value::as_array)
            .map(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .filter(|model| render::valid_text(model, 64))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        // A premium model is refused here as the service would refuse it; an
        // older id the catalog still accepts is known.
        if let Some(error) = discovery::premium_in_catalog(&body, model, self.mode) {
            return Err(error);
        }
        let known = offered.contains(&model)
            || body.get("default_model").and_then(Value::as_str) == Some(model)
            || discovery::accepted_in_catalog(&body, model);
        if offered.is_empty() || known {
            return Ok(());
        }
        let nearest = nearest(model, &offered, 3);
        let list = self.command("model list");
        let mut error = RunnerError::invocation(format!(
            "--model {model_quoted} is not a model this account may run; nearest: {}; `{list}` lists them all",
            nearest.join(", "),
            model_quoted = crate::error::quote(model)
        ));
        let argv = self.argv_with_option("--model", nearest[0]);
        error.action = format!(
            "Rerun with a model the service offers, for example `{}`.",
            render::argv_text(&argv)
        );
        Err(error.with_detail("suggestedArgv", json!(argv)))
    }

    /// This invocation's argv with the value of `option` replaced.
    pub fn argv_with_option(&self, option: &str, value: &str) -> Vec<String> {
        let mut argv = self.invocation.argv.clone();
        let prefix = format!("{option}=");
        let mut index = 0;
        while index < argv.len() {
            if argv[index] == option && index + 1 < argv.len() {
                value.clone_into(&mut argv[index + 1]);
                index += 1;
            } else if argv[index].starts_with(&prefix) {
                argv[index] = format!("{prefix}{value}");
            }
            index += 1;
        }
        argv
    }

    /// This invocation's argv with `option` set to `value`: replaced when
    /// given, otherwise added before any `--`.
    pub fn argv_setting_option(&self, option: &str, value: &str) -> Vec<String> {
        if self.invocation.option(option).is_some() {
            return self.argv_with_option(option, value);
        }
        let mut argv = self.invocation.argv.clone();
        let end = argv
            .iter()
            .position(|token| token == "--")
            .unwrap_or(argv.len());
        argv.splice(end..end, [option.to_owned(), value.to_owned()]);
        argv
    }

    /// This invocation's argv with its last `given` token (a positional
    /// argument as typed) replaced by `value`.
    pub fn argv_with_argument(&self, given: &str, value: &str) -> Vec<String> {
        let mut argv = self.invocation.argv.clone();
        if let Some(index) = argv.iter().rposition(|token| token == given) {
            value.clone_into(&mut argv[index]);
        }
        argv
    }

    /// A handler's value error with an exact fix: the Action
    /// quotes the corrected argv, which is also `details.suggestedArgv`.
    pub fn corrected(
        &self,
        mut error: RunnerError,
        action: &str,
        argv: Vec<String>,
    ) -> RunnerError {
        let mut text = action.replace("{command}", &render::argv_text(&argv));
        if !text.ends_with('.') {
            text.push('.');
        }
        error.action = text;
        error.with_detail("suggestedArgv", json!(argv))
    }

    /// A `--limit` error: when `raw` is a whole number, the
    /// suggestion is the nearest value from 1 to `max`; any other value
    /// keeps the operation's help.
    pub fn limit_error(&self, error: RunnerError, raw: &str, max: u64) -> RunnerError {
        let (negative, digits) = raw
            .strip_prefix('-')
            .map_or((false, raw), |digits| (true, digits));
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return error;
        }
        let significant = digits.trim_start_matches('0');
        let nearest = if negative || significant.is_empty() {
            1
        } else if significant.len() > 18 {
            max
        } else {
            significant
                .parse::<u64>()
                .map_or(max, |value| value.clamp(1, max))
        };
        let which = match nearest {
            1 => "smallest",
            value if value == max => "largest",
            _ => "same",
        };
        let command = format!("cli {}", command_words(self.operation).join(" "));
        let action = if which == "same" {
            "Write --limit as a plain whole number: `{command}`".to_owned()
        } else {
            format!("Use --limit {nearest}, the {which} value `{command}` accepts: `{{command}}`")
        };
        self.corrected(
            error,
            &action,
            self.argv_with_option("--limit", &nearest.to_string()),
        )
    }

    /// The advisory `GET /run/quote` hold for a plan (`index` is the
    /// operation's quote request): `{hold}`, or `None` when the quote fails,
    /// so a failed quote never hides the plan. The service's price policy
    /// reference stays internal.
    pub fn advisory_quote(&mut self, index: usize, environment: Option<&str>) -> Option<Value> {
        let mut request = http::Request::from_manifest(self.operation, index, "/run/quote")
            .class(http::TransportClass::Control);
        if let Some(environment) = environment {
            request = request.query("environment", environment.to_owned());
        }
        let body = self.send(&request).ok()?.json_object().ok()?;
        let hold = body.get("hold")?;
        let hold_usd = hold["hold_usd"].as_str()?;
        let hold_cents = render::usd_cents(hold_usd)?;
        let ttl = hold["ttl_seconds"].as_u64()?;
        Some(json!({
            "hold": {"hold_usd": hold_usd, "hold_cents": hold_cents, "ttl_seconds": ttl},
        }))
    }

    /// The confirmation gate: `--preview` returns the plan as the result,
    /// a confirm-class operation without `--yes` fails with
    /// `CONFIRMATION_REQUIRED`, otherwise the caller proceeds.
    pub fn gate(&self, planned: Value) -> Result<Gate, RunnerError> {
        if self.invocation.preview {
            return Ok(Gate::Preview(
                json!({"preview": true, "plannedRequest": planned}),
            ));
        }
        if self.operation["confirm"] == true && !self.invocation.yes {
            let mut confirm = self.invocation.argv.clone();
            confirm.push("--yes".into());
            let mut preview = self.invocation.argv.clone();
            preview.push("--preview".into());
            let mut error = RunnerError::catalog(ErrorCode::ConfirmationRequired);
            // The consequence that needs --yes (manifest `confirmReason`).
            if let Some(reason) = self.operation["confirmReason"].as_str() {
                error = error.with_detail("reason", format!("--yes is required because {reason}"));
            }
            return Err(error
                .with_detail("plannedRequest", planned)
                .with_detail("confirmArgv", json!(confirm))
                .with_detail("previewArgv", json!(preview)));
        }
        Ok(Gate::Proceed)
    }

    /// Default body of a not-yet-implemented feature handler.
    ///
    /// - A confirm or preview operation whose first always-sent mutation has
    ///   no body and no query is planned generically (path parameters from
    ///   positional arguments in order), so `--preview` and
    ///   `CONFIRMATION_REQUIRED` work before the feature lands. It never sends
    ///   the mutation.
    /// - Otherwise the first always-sent GET without a query is sent, so
    ///   transport limits and error classification (framework behavior) are
    ///   observable; a 2xx result still needs the feature's projection.
    pub fn not_implemented(&mut self) -> Result<Value, RunnerError> {
        if self.invocation.preview || self.operation["confirm"] == true {
            if let Some((index, path)) = self.generic_path(false) {
                let planned = self.planned(index, &path, &[], None);
                if let Gate::Preview(result) = self.gate(planned)? {
                    return Ok(result);
                }
            }
        } else if let Some((index, path)) = self.generic_path(true) {
            self.send(&http::Request::from_manifest(self.operation, index, path))?;
        }
        Err(RunnerError::catalog(ErrorCode::InternalRunnerFault)
            .with_detail("reason", "not implemented"))
    }

    fn generic_path(&self, read: bool) -> Option<(usize, String)> {
        let requests = self.operation["requests"].as_array()?;
        let index = requests.iter().position(|request| {
            (request["method"] == "GET") == read && request["when"] == "always"
        })?;
        let request = &requests[index];
        if !request["body"].is_null() || request.get("query").is_some_and(|q| !q.is_null()) {
            return None;
        }
        let mut values = self.operation["arguments"]
            .as_array()?
            .iter()
            .filter_map(|argument| argument["name"].as_str())
            .filter_map(|name| self.argument(name));
        let mut path = String::new();
        for segment in request["path"].as_str()?.split('/').skip(1) {
            path.push('/');
            if segment.starts_with('{') {
                path.push_str(&http::encode_segment(values.next()?));
            } else {
                path.push_str(segment);
            }
        }
        Some((index, path))
    }
}

/// Why a credential failed the strict predicate, without echoing it.
/// The Action of a failed credential. It follows where the key
/// came from: an environment key is replaced or unset (`cli auth login` would
/// not change it), a stored key is renewed by login, and a missing key names
/// both ways to supply one.
pub(crate) fn credential_action(
    environment: &Environment,
    mode: OutputMode,
    source: &str,
) -> String {
    let variable = environment.credential_env;
    let login = || render::follow_up_command(mode, "auth login");
    match source {
        "environment" => format!("Replace or unset {variable}, then retry."),
        "store" => format!("Run `{}` again, or set {variable}, then retry.", login()),
        _ => format!("Set {variable} or run `{}`, then retry.", login()),
    }
}

/// `SERVICE_AUTH_REQUIRED` for a credential problem (`missing`, `malformed`
/// or `rejected`) with its source, variable, reason and source-specific Action.
pub(crate) fn credential_failure(
    environment: &Environment,
    mode: OutputMode,
    source: &str,
    problem: &str,
    reason: String,
) -> RunnerError {
    let mut error = RunnerError::catalog(ErrorCode::ServiceAuthRequired)
        .with_detail("reason", reason)
        .with_detail("credentialSource", source.to_owned())
        .with_detail("credentialVariable", environment.credential_env)
        .with_detail("credentialProblem", problem.to_owned());
    error.action = credential_action(environment, mode, source);
    error
}

/// A `CREDENTIAL_STORE_UNAVAILABLE` that names the variable to set instead.
///
/// A failure the store already explained (the macOS keychain-access and
/// outcome-unknown cases) keeps its own reason and Action.
pub(crate) fn store_unavailable(error: RunnerError, environment: &Environment) -> RunnerError {
    if error
        .details
        .as_ref()
        .is_some_and(|details| details.contains_key("reason"))
    {
        return error;
    }
    let variable = environment.credential_env;
    let mut error = error.with_detail(
        "reason",
        format!("the OS credential store is unavailable; set {variable} for this command"),
    );
    error.action = format!(
        "Set {variable} for this command, or unlock or configure the operating system credential store, then retry."
    );
    error
}

/// Why no key was found.
pub(crate) fn missing_reason(environment: &Environment, mode: OutputMode) -> String {
    format!(
        "no API key for {}: set {} or run `{}`",
        environment.label(),
        environment.credential_env,
        render::follow_up_command(mode, "auth login")
    )
}

/// Why the service refused the key (401, or 403 `Invalid API key.`).
pub(crate) fn rejected_reason(environment: &Environment, mode: OutputMode, source: &str) -> String {
    let variable = environment.credential_env;
    let login = render::follow_up_command(mode, "auth login");
    if source == "environment" {
        format!(
            "the service rejected the API key in {variable} (unknown or revoked); replace or unset {variable}: an environment key takes precedence over `{login}`"
        )
    } else {
        format!(
            "the service rejected the API key stored for {} (unknown or revoked); run `{login}` again",
            environment.label()
        )
    }
}

/// Rewrites every `` `prose cli ...` `` command in a fixed hint so it keeps
/// the machine output mode (the account-command twin of
/// [`Context::localize`]).
pub(crate) fn localize_hint(mode: OutputMode, text: &str) -> String {
    text.replace(
        "`prose cli ",
        &format!("`{} ", render::follow_up_command(mode, "")),
    )
}

pub(crate) fn malformed_reason(
    token: &str,
    variable: &str,
    from_environment: bool,
    environment: &Environment,
) -> String {
    let shape = "an OpenProse API key is rr_test, an underscore and 32 lowercase hex digits";
    if !from_environment {
        return format!(
            "the key stored for {} is not a valid OpenProse API key ({shape}); run `prose cli auth login` again",
            environment.label()
        );
    }
    if token.starts_with("rr_live_") {
        return format!(
            "the {variable} value starts with rr_live_, which is not an OpenProse API key ({shape}); replace or unset {variable}"
        );
    }
    format!(
        "the {variable} value is not a valid OpenProse API key ({shape}); replace or unset {variable}: an environment key takes precedence over `prose cli auth login`"
    )
}

/// Resolves the service for an invocation (see [`Environment::resolve`]).
fn resolve_environment(system: &SystemContext) -> Result<Environment, RunnerError> {
    Environment::resolve(&system.environment)
}

fn resolve_mode(
    output: Option<OutputMode>,
    json: bool,
    globals: &GlobalFlags,
    system: &SystemContext,
) -> Result<OutputMode, RunnerError> {
    if let (Some(left), Some(right)) = (globals.output, output) {
        if left != right {
            return Err(taught(
                "--output was given twice with different values",
                "Pass --output once, with the mode you mean.",
            ));
        }
    }
    if json {
        if globals.output.is_some_and(|mode| mode != OutputMode::Json) {
            return Err(taught(
                "--json conflicts with --output",
                "Keep one output choice: drop --json or the --output before cli.",
            ));
        }
        return Ok(OutputMode::Json);
    }
    if let Some(mode) = output.or(globals.output) {
        return Ok(mode);
    }
    match system.environment.get("PROSE_OUTPUT") {
        Some(value) => OutputMode::parse(value).map_err(|_| {
            RunnerError::config(format!(
                "invalid output mode {value_quoted}; expected human, json, or jsonl",
                value_quoted = crate::error::quote(value)
            ))
            .with_detail("source", "PROSE_OUTPUT")
        }),
        None => Ok(OutputMode::Human),
    }
}

/// An invocation error whose Action is fixed text.
fn taught(reason: &str, action: &str) -> RunnerError {
    let mut error = invalid(reason);
    action.clone_into(&mut error.action);
    error
}

/// Applies a correction: the per-cause Action and, when a command applies,
/// `details.suggestedArgv` rendered by the shared follow-up renderer.
pub fn teach(mut error: RunnerError, correction: &Correction, mode: OutputMode) -> RunnerError {
    let (action, argv) = match correction {
        Correction::Text(text) => (text.clone(), None),
        Correction::Argv { action, argv } => (action.clone(), Some(argv.clone())),
        Correction::Command { action, words } => {
            let words = words.iter().map(String::as_str).collect::<Vec<_>>();
            (action.clone(), Some(render::follow_up_argv(mode, &words)))
        }
    };
    let mut action = match &argv {
        Some(argv) => action.replace("{command}", &render::argv_text(argv)),
        None => action,
    };
    if !action.ends_with('.') {
        action.push('.');
    }
    error.action = action;
    match argv {
        Some(argv) => error.with_detail("suggestedArgv", json!(argv)),
        None => error,
    }
}

/// Whether an error still carries the generic taxonomy Action of
/// `INVOCATION_INVALID` (a handler's value check), which the service
/// replaces with a per-operation one.
fn generic_invocation(error: &RunnerError) -> bool {
    error.code == ErrorCode::InvocationInvalid
        && error.action == RunnerError::catalog(ErrorCode::InvocationInvalid).action
}

/// The Action for a handler's value check: the operation's own help.
fn value_correction(operation: &Value) -> Correction {
    Correction::Command {
        action: "Correct the value named in Detail; `{command}` shows the accepted syntax"
            .to_owned(),
        words: help_words(&command_words(operation)),
    }
}

/// The token span of a runner-global option before `cli` in `argv`.
fn global_span(argv: &[String], name: &str, takes_value: bool) -> Option<(usize, usize)> {
    let cli = argv.iter().position(|token| token == "cli")?;
    argv[..cli]
        .iter()
        .position(|token| token == name)
        .map_or_else(
            || {
                argv[..cli]
                    .iter()
                    .position(|token| token.starts_with(&format!("{name}=")))
                    .map(|at| (at, 1))
            },
            |at| Some((at, if takes_value { 2 } else { 1 })),
        )
}

/// Runner-global options other than these are not accepted by service
/// operations; `--model`, `--harness` and `--dry-run` get a targeted reason
/// and a corrected argv.
fn check_globals(
    globals: &GlobalFlags,
    operation: &Value,
    argv: &[String],
) -> Result<(), RunnerError> {
    let has_option = |name: &str| {
        operation["options"]
            .as_array()
            .is_some_and(|options| options.iter().any(|option| option["name"] == name))
    };
    let corrected =
        |error: RunnerError, action: String, span: Option<(usize, usize)>, append: &[String]| {
            let correction = match span {
                Some((at, len)) => {
                    let mut fixed = argv[..at].to_vec();
                    fixed.extend_from_slice(&argv[at + len..]);
                    fixed.extend_from_slice(append);
                    Correction::Argv {
                        action,
                        argv: fixed,
                    }
                }
                None => Correction::Text(action.replace(": `{command}`", "")),
            };
            teach(error, &correction, OutputMode::Human)
        };
    if let Some(model) = &globals.model {
        let error = invalid(
            "the runner option --model does not apply to service operations; pass --model after the command, for example `cli run submit --model MODEL`",
        );
        let span = global_span(argv, "--model", true);
        return Err(if has_option("--model") {
            corrected(
                error,
                "Pass --model after the command: `{command}`".to_owned(),
                span,
                &["--model".to_owned(), model.clone()],
            )
        } else {
            corrected(
                error,
                "Drop --model; this command takes no model: `{command}`".to_owned(),
                span,
                &[],
            )
        });
    }
    if globals.harness.is_some() {
        return Err(corrected(
            invalid(
                "the runner option --harness does not apply to service operations; hosted runs use `cli run submit`",
            ),
            "Drop --harness; the hosted service chooses where a run executes: `{command}`"
                .to_owned(),
            global_span(argv, "--harness", true),
            &[],
        ));
    }
    if globals.dry_run {
        let error = invalid(
            "the runner option --dry-run does not apply to service operations; use --preview after the command",
        );
        let span = global_span(argv, "--dry-run", false);
        return Err(if operation["preview"] == true {
            corrected(
                error,
                "Use --preview after the command instead: `{command}`".to_owned(),
                span,
                &["--preview".to_owned()],
            )
        } else {
            corrected(
                error,
                "Drop --dry-run; this command only reads: `{command}`".to_owned(),
                span,
                &[],
            )
        });
    }
    let mut other = globals.clone();
    other.output = None;
    other.no_color = false;
    other.verbose = false;
    if other != GlobalFlags::default() {
        return Err(taught(
            "service operations accept only --output, --no-color and --verbose before cli",
            "Remove the other runner options from before cli; service operations take their own options after the command.",
        ));
    }
    Ok(())
}

/// Prints the custom-endpoint banner of a `dev-endpoint` build once, before
/// the first streamed byte. A failure that streamed nothing
/// prints no separate banner: its error line already starts with the label.
/// Production prints none.
struct Banner<'w> {
    err: std::cell::RefCell<&'w mut dyn Write>,
    pending: std::cell::RefCell<Option<String>>,
}

impl Banner<'_> {
    fn emit(&self) -> std::io::Result<()> {
        if let Some(text) = self.pending.borrow_mut().take() {
            self.err.borrow_mut().write_all(text.as_bytes())?;
        }
        Ok(())
    }
}

struct BannerErr<'b, 'w>(&'b Banner<'w>);

impl Write for BannerErr<'_, '_> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        if !buf.is_empty() {
            self.0.emit()?;
        }
        self.0.err.borrow_mut().write(buf)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0.err.borrow_mut().flush()
    }
}

struct BannerOut<'b, 'w> {
    banner: &'b Banner<'w>,
    out: &'w mut dyn Write,
}

impl Write for BannerOut<'_, '_> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        if !buf.is_empty() {
            self.banner.emit()?;
        }
        self.out.write(buf)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.out.flush()
    }
}

/// Renders a service-noun invocation that named no operation.
fn execute_invalid(
    rejected: &ServiceInvalid,
    globals: &GlobalFlags,
    system: &SystemContext,
) -> CommandOutcome {
    let mode =
        resolve_mode(rejected.output, rejected.json, globals, system).unwrap_or(if rejected.json {
            OutputMode::Json
        } else {
            rejected.output.or(globals.output).unwrap_or_default()
        });
    let environment = resolve_environment(system).ok();
    let mut error = rejected
        .error
        .clone()
        .unwrap_or_else(|| invalid("unknown service command"));
    if let Some(correction) = &rejected.correction {
        error = teach(error, correction, mode);
    }
    // A runner command (`cli doctor`, `cli harness ...`) keeps the runner
    // error document, like the language.
    if runner_argv(&rejected.argv) && mode != OutputMode::Human {
        return CommandOutcome::json(&error, error.exit_code);
    }
    render::rejected(
        command_operation(&rejected.argv),
        environment.as_ref(),
        mode,
        error,
    )
}

/// Whether the command word after the first `cli` is a runner command
/// (`doctor`, `harness`, `cleanup`, `config`), not a manifest noun.
pub fn runner_argv(argv: &[String]) -> bool {
    argv.iter()
        .take_while(|token| *token != "--")
        .position(|token| token == "cli")
        .and_then(|at| argv.get(at + 1))
        .is_some_and(|word| RUNNER_COMMANDS.iter().any(|path| path[0] == word))
}

/// The manifest operation an argv names: the longest run of
/// command words right after the first `cli` (before any `--`) that equals
/// an operation's command. `None` when there is no `cli` or the words name no
/// operation (`cli models`, `cli run delete`); the envelope then says `cli`.
pub fn command_operation(argv: &[String]) -> Option<&'static Value> {
    let start = argv
        .iter()
        .take_while(|token| *token != "--")
        .position(|token| token == "cli")?
        + 1;
    let words = argv[start..]
        .iter()
        .take_while(|token| !token.starts_with('-'))
        .collect::<Vec<_>>();
    operations()
        .iter()
        .filter(|operation| {
            let command = command_words(operation);
            command.len() <= words.len()
                && command
                    .iter()
                    .zip(&words)
                    .all(|(left, right)| left == *right)
        })
        .max_by_key(|operation| command_words(operation).len())
}

/// Whether an argv is a service command line: after the
/// runner-global prefix comes `cli` and then a manifest noun. Runner
/// commands (`cli doctor`, `cli harness ...`) and language argvs are not.
pub fn service_argv(argv: &[String]) -> bool {
    let mut index = 0;
    while let Some(token) = argv.get(index) {
        if token == "--" {
            return false;
        }
        if token == "cli" {
            return argv.get(index + 1).is_some_and(|noun| {
                operations()
                    .iter()
                    .any(|operation| command_words(operation).first() == Some(&noun.as_str()))
            });
        }
        if !token.starts_with('-') {
            return false;
        }
        index += if !token.contains('=') && crate::invocation::is_value_option(token) {
            2
        } else {
            1
        };
    }
    false
}

/// Renders an invocation error that the entry point raised outside the
/// service parser (the runner-global prefix, a service account path) for a
/// service argv in JSON or JSONL mode: the same envelope service errors use
///. `None` leaves the error to the runner renderer: human
/// mode, other error codes, and argvs that are not service command lines.
pub fn argv_error_outcome(
    argv: &[String],
    error: &RunnerError,
    mode: OutputMode,
    system: Option<&SystemContext>,
) -> Option<CommandOutcome> {
    if mode == OutputMode::Human
        || !matches!(
            error.code,
            ErrorCode::InvocationInvalid | ErrorCode::ConfirmationRequired
        )
        || !service_argv(argv)
    {
        return None;
    }
    Some(render::rejected(
        command_operation(argv),
        system
            .map_or_else(|| Ok(Environment::production()), resolve_environment)
            .ok()
            .as_ref(),
        mode,
        error.clone(),
    ))
}

/// Executes a service service command. Streamed lines go to `out`; the
/// returned outcome carries the final result or error.
pub fn execute(
    command: &ServiceCommand,
    globals: &GlobalFlags,
    system: &SystemContext,
    cancellation: &CancellationToken,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> CommandOutcome {
    render::configure_run_errors(&system.environment);
    let invocation = match command {
        ServiceCommand::Help(text) => {
            let mode = resolve_mode(None, false, globals, system).unwrap_or_default();
            return help_outcome(text, mode);
        }
        ServiceCommand::Invalid(invalid) => return execute_invalid(invalid, globals, system),
        ServiceCommand::Invoke(invocation) => invocation,
    };
    let operation = operation(&invocation.operation).expect("parsed operations exist");
    let fallback_mode = if invocation.json {
        OutputMode::Json
    } else {
        invocation.output.or(globals.output).unwrap_or_default()
    };
    let environment = match resolve_environment(system) {
        Ok(environment) => environment,
        Err(error) => return render::rejected(Some(operation), None, fallback_mode, error),
    };
    let mode = match resolve_mode(invocation.output, invocation.json, globals, system) {
        Ok(mode) => mode,
        Err(error) => {
            return render::outcome(operation, &environment, fallback_mode, Err(error), None);
        }
    };
    if let Some(error) = invocation.error.clone() {
        let error = match &invocation.correction {
            Some(correction) => teach(error, correction, mode),
            None => error,
        };
        return render::outcome(operation, &environment, mode, Err(error), None);
    }
    if let Err(error) = check_globals(globals, operation, &invocation.argv) {
        return render::outcome(operation, &environment, mode, Err(error), None);
    }
    let id = operation["id"].as_str().unwrap_or_default();
    if mode == OutputMode::Human {
        if let Some(text) = static_view(id) {
            return CommandOutcome::human(text.to_owned(), "", 0);
        }
    } else if let Some(result) = static_result(id) {
        return render::outcome(operation, &environment, mode, Ok(result), None);
    }
    if operation["id"] == "service.guide" {
        let extras = render::Extras {
            human: Some(GUIDE_TEXT.to_owned()),
            ..render::Extras::default()
        };
        return render::outcome(
            operation,
            &environment,
            mode,
            Ok(guide_result()),
            Some(extras),
        );
    }
    let transport = match http::Transport::new(system, &environment, cancellation) {
        Ok(transport) => transport,
        Err(error) => return render::outcome(operation, &environment, mode, Err(error), None),
    };
    let banner = Banner {
        err: std::cell::RefCell::new(err),
        pending: std::cell::RefCell::new(
            (mode == OutputMode::Human && environment.is_custom())
                .then(|| format!("{}\n", environment.label())),
        ),
    };
    let mut out_sink = BannerOut {
        banner: &banner,
        out,
    };
    let mut err_sink = BannerErr(&banner);
    let mut context = Context {
        invocation,
        operation,
        environment: environment.clone(),
        mode,
        transport,
        system,
        cancellation: cancellation.clone(),
        out: &mut out_sink,
        err: &mut err_sink,
        next_before: None,
        run_id: None,
        human: None,
        verified_owner: None,
        credential: None,
        credential_source: None,
        key_rejected: std::cell::Cell::new(false),
    };
    let mut result = context
        .transport
        .prepare_journal(&context.journal())
        .and_then(|()| dispatch(&mut context));
    if let Err(error) = context.transport.finish() {
        result = Err(error);
    }
    if let Err(error) = &mut result {
        context.annotate_auth(error);
        render::redact_error(error, context.known_credential());
        if generic_invocation(error) {
            *error = teach(error.clone(), &value_correction(operation), mode);
        }
        let argument = |name: &str| context.argument(name).map(str::to_owned);
        *error = not_found::explain_operation_not_found(error.clone(), operation, &argument, mode);
        let help = help_words(&command_words(operation));
        let help = help.iter().map(String::as_str).collect::<Vec<_>>();
        *error = not_found::explain_rejected(error.clone(), mode, &help);
    }
    let extras = render::Extras {
        next_before: context.next_before.take(),
        run_id: context.run_id.take(),
        human: context.human.take(),
        page_words: page_words(operation, invocation),
    };
    drop(context);
    if result.is_ok() {
        let _ = banner.emit();
    }
    render::outcome(operation, &environment, mode, result, Some(extras))
}

fn dispatch(context: &mut Context<'_>) -> Result<Value, RunnerError> {
    let feature = context.operation["feature"].as_str().unwrap_or_default();
    match feature {
        "discovery" => discovery::execute(context),
        "runs" => runs::execute(context),
        "run-records" => run_records::execute(context),
        "programs" => programs::execute(context),
        "results" => results::execute(context),
        "jobs" => jobs::execute(context),
        "wallet" => wallet::execute(context),
        "organizations" => organizations::execute(context),
        _ => context.not_implemented(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    /// The envelope of a rejected argv names the operation the
    /// argv names, and only service command lines take the envelope.
    #[test]
    fn rejected_argvs_name_their_operation_and_shape() {
        let named = |args: &[&str]| {
            command_operation(&strings(args))
                .map(|operation| operation["id"].as_str().unwrap().to_owned())
        };
        assert_eq!(
            named(&["--json", "cli", "run", "list"]).as_deref(),
            Some("run.list")
        );
        assert_eq!(
            named(&["cli", "org", "member", "list", "acme"]).as_deref(),
            Some("org.member.list")
        );
        assert_eq!(named(&["cli", "api", "GET", "/runs"]), None);
        assert_eq!(named(&["cli", "environment", "show"]), None);
        assert_eq!(named(&["cli", "models"]), None);
        assert_eq!(named(&["cli", "org", "member", "rm", "acme"]), None);
        assert_eq!(named(&["models", "--json"]), None);
        assert_eq!(named(&["--", "cli", "run", "list"]), None);
        assert!(service_argv(&strings(&[
            "--output", "json", "cli", "package", "frob"
        ])));
        assert!(service_argv(&strings(&[
            "--output=json",
            "cli",
            "run",
            "list"
        ])));
        assert!(!service_argv(&strings(&[
            "--output", "json", "cli", "doctor", "--bogus"
        ])));
        assert!(!service_argv(&strings(&["--cwd", "cli", "run", "list"])));
        assert!(!service_argv(&strings(&["run.prose", "cli", "run"])));
        assert!(runner_argv(&strings(&[
            "--output", "json", "cli", "harness", "lst"
        ])));
        assert!(!runner_argv(&strings(&["cli", "run", "list"])));
        let error = invalid("unknown command `cli models`");
        let outcome = render::rejected(None, None, OutputMode::Json, error.clone());
        let crate::output::Payload::Json(document) = outcome.payload else {
            panic!("JSON mode renders one JSON document");
        };
        assert_eq!(outcome.exit_code, 2);
        assert_eq!(document["schema"], "openprose.service-operation/1");
        assert_eq!(document["operation"], "cli");
        assert!(document.get("environment").is_none());
        assert_eq!(document["interaction"], Value::Null);
        assert_eq!(document["result"], Value::Null);
        // A service command's invocation error says what was wrong.
        let mut plain = serde_json::to_value(&error).unwrap();
        plain["message"] = json!("Unknown command.");
        assert_eq!(document["problem"], plain);
    }

    fn reason(error: &RunnerError) -> String {
        error.details.as_ref().unwrap()["reason"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    fn invoke(args: &[&str]) -> ServiceInvocation {
        match parse(&strings(args), &strings(args)).unwrap() {
            ServiceCommand::Invoke(invocation) => *invocation,
            ServiceCommand::Help(_) => panic!("help"),
            ServiceCommand::Invalid(_) => panic!("invalid"),
        }
    }

    fn rejected(args: &[&str]) -> ServiceInvalid {
        match parse(&strings(args), &strings(args)).unwrap() {
            ServiceCommand::Invalid(invalid) => *invalid,
            other => panic!("invalid expected, got {other:?}"),
        }
    }

    #[test]
    fn manifest_and_help_are_embedded_byte_for_byte() {
        assert_eq!(manifest()["schema"], "openprose.service-operations/1");
        assert!(help_topics().contains_key("cli run submit"));
        assert!(MANIFEST_TEXT.ends_with('\n') || MANIFEST_TEXT.ends_with('}'));
    }

    #[test]
    fn service_operations_and_capabilities_views_follow_the_mode() {
        // Human is the summary table or page; JSON modes print the manifest
        // or the capabilities document as the envelope's result.
        let table = static_view("service.operations").unwrap();
        assert!(table.starts_with("OPERATION "));
        let published = static_result("service.operations").unwrap();
        assert_eq!(
            published["operations"].as_array().unwrap().len(),
            manifest()["operations"].as_array().unwrap().len()
        );
        for key in [
            "clientIdentity",
            "interactionsSource",
            "journal",
            "transportClasses",
            "credential",
        ] {
            assert!(published.get(key).is_none(), "{key}");
        }
        let text = published.to_string();
        for forbidden in public_projection()["forbid"].as_array().unwrap() {
            assert!(!text.contains(forbidden.as_str().unwrap()), "{forbidden}");
        }
        let capabilities = static_result("service.capabilities").unwrap();
        assert_eq!(capabilities["schema"], "openprose.service-capabilities/1");
        assert_eq!(capabilities["exitCodes"], manifest()["exitCodes"]);
        assert!(static_view("service.status").is_none());
        assert!(static_result("service.status").is_none());
    }

    #[test]
    fn service_guide_sections_follow_the_guide_file() {
        // Every `## ` line starts a section, ids are slugs, and
        // the section bodies plus headings rebuild the file after the title.
        assert_eq!(guide_slug("Get a run's answer"), "get-a-runs-answer");
        assert_eq!(
            guide_slug("Exit codes, resume and detach"),
            "exit-codes-resume-and-detach"
        );
        let result = guide_result();
        let sections = result["sections"].as_array().unwrap();
        let ids = sections
            .iter()
            .map(|s| s["id"].as_str().unwrap())
            .collect::<Vec<_>>();
        for wanted in [
            "grammar",
            "credentials",
            "confirm-and-preview",
            "exit-codes-resume-and-detach",
            "paging",
            "get-a-runs-answer",
            "recipes",
        ] {
            assert!(ids.contains(&wanted), "{wanted}");
        }
        let rebuilt = sections
            .iter()
            .map(|s| {
                format!(
                    "## {}\n\n{}\n",
                    s["title"].as_str().unwrap(),
                    s["body"].as_str().unwrap()
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(
            format!("# OpenProse service guide\n\n{rebuilt}"),
            GUIDE_TEXT
        );
    }

    #[test]
    fn routes_only_service_operation_paths() {
        assert!(claims(&strings(&["run", "list"])));
        assert!(claims(&strings(&["org", "show", "x"])));
        assert!(!claims(&strings(&["org", "list"])));
        assert!(!claims(&strings(&["auth", "status"])));
        assert!(!claims(&strings(&["doctor"])));
    }

    #[test]
    fn parses_arguments_options_and_common_flags() {
        let parsed = invoke(&[
            "run",
            "submit",
            "prog.md",
            "--input",
            "a=1",
            "--input=b=2",
            "--detach",
            "--yes",
            "--json",
        ]);
        assert_eq!(parsed.operation, "run.submit");
        assert_eq!(parsed.argument("FILE"), Some("prog.md"));
        assert_eq!(parsed.option_values("--input"), ["a=1", "b=2"]);
        assert!(parsed.flag("--detach") && parsed.yes && parsed.json);
        assert!(parsed.error.is_none());
    }

    #[test]
    fn unknown_options_and_commands_suggest_a_fix() {
        let parsed = invoke(&["run", "cancel", "run_1", "--yess"]);
        assert_eq!(
            reason(parsed.error.as_ref().unwrap()),
            "unknown option --yess for `cli run cancel`; did you mean --yes?"
        );
        let invalid = rejected(&["run", "submt"]);
        assert!(reason(invalid.error.as_ref().unwrap()).contains("did you mean `cli run submit`?"));
        // Its arguments are optional, and the handler needs a
        // program, so the bare correction is its help.
        assert_eq!(
            invalid.correction,
            Some(Correction::Argv {
                action: "Use `cli run submit`; `{command}` shows what it needs".into(),
                argv: strings(&["run", "submit", "--help"]),
            })
        );
        let invalid = rejected(&["run"]);
        assert!(reason(invalid.error.as_ref().unwrap()).starts_with("`cli run` needs a command:"));
    }

    #[test]
    fn corrections_are_per_cause_and_keep_the_output_mode() {
        // A missing <RUN_ID> points at the run listing in the same output mode.
        let parsed = invoke(&["run", "show"]);
        let error = teach(
            parsed.error.unwrap(),
            parsed.correction.as_ref().unwrap(),
            OutputMode::Json,
        );
        assert_eq!(
            error.action,
            "Pass <RUN_ID>; list runs with `prose --output json cli run list`."
        );
        assert_eq!(
            error.details.as_ref().unwrap()["suggestedArgv"],
            json!(["--output", "json", "cli", "run", "list"])
        );
        assert!(!error.action.contains("place global options"));
        // A trailing output mode after a rejected token is still found.
        let parsed = invoke(&["run", "show", "--bogus", "--output", "jsonl"]);
        assert_eq!(parsed.output, Some(OutputMode::Jsonl));
        // The unknown option is replaced in a copy of the original argv.
        let original = strings(&[
            "--output", "json", "cli", "run", "cancel", "run_1", "--yess",
        ]);
        let ServiceCommand::Invoke(parsed) = parse(&original[3..], &original).unwrap() else {
            panic!("invoke expected");
        };
        assert_eq!(
            parsed.correction,
            Some(Correction::Argv {
                action: "Replace --yess with --yes: `{command}`".into(),
                argv: strings(&["--output", "json", "cli", "run", "cancel", "run_1", "--yes"]),
            })
        );
    }

    /// Every parser suggestion is a complete command.
    #[test]
    fn suggestions_complete_to_a_command_that_parses() {
        let argv_of = |correction: Option<Correction>| match correction {
            Some(Correction::Argv { argv, .. }) => argv,
            Some(Correction::Command { words, .. }) => words,
            other => panic!("a command correction expected, got {other:?}"),
        };
        let unknown_argv = |args: &[&str]| match unknown(&strings(args), &strings(args)) {
            ServiceCommand::Invalid(invalid) => argv_of(invalid.correction),
            other => panic!("invalid expected, got {other:?}"),
        };
        assert_eq!(unknown_argv(&["models"]), strings(&["model", "list"]));
        assert_eq!(unknown_argv(&["organization"]), strings(&["org", "--help"]));
        assert_eq!(unknown_argv(&["money"]), strings(&["wallet", "balance"]));
        assert_eq!(unknown_argv(&["list", "jobs"]), strings(&["job", "list"]));
        assert_eq!(
            argv_of(rejected(&["model"]).correction),
            strings(&["model", "list"])
        );
        assert_eq!(
            argv_of(rejected(&["wallet", "show"]).correction),
            strings(&["wallet", "balance"])
        );
        assert_eq!(
            argv_of(rejected(&["run", "result", "run_1"]).correction),
            strings(&["run", "show", "run_1", "--file", "outputs/result.json"])
        );
        // Settled: `cli run show` without RUN_ID is itself rejected, so the
        // suggestion is the listing that finds one (settling needs the `cli`
        // of the original argv).
        let original = strings(&["cli", "run", "shw"]);
        let ServiceCommand::Invalid(settled) = parse(&original[1..], &original).unwrap() else {
            panic!("invalid expected");
        };
        assert_eq!(argv_of(settled.correction), strings(&["run", "list"]));
        assert_eq!(
            argv_of(invoke(&["wallet", "topup", "500"]).correction),
            strings(&["wallet", "topup", "--amount-cents", "500"])
        );
        assert_eq!(
            argv_of(invoke(&["wallet", "topup", "--amount", "5", "--preview"]).correction),
            strings(&["wallet", "topup", "--amount-cents", "500", "--preview"])
        );
        let redeem = invoke(&["wallet", "redeem", "SECRET-CODE", "--yes"]);
        assert!(!reason(redeem.error.as_ref().unwrap()).contains("SECRET-CODE"));
        assert_eq!(
            argv_of(redeem.correction),
            strings(&["wallet", "redeem", "--code-file", "-", "--yes"])
        );
        let update = invoke(&["job", "update", "j1", "--name", "x", "--yes"]);
        assert_eq!(
            update.error.as_ref().unwrap().details.as_ref().unwrap()["suggestedStdin"],
            "{\"name\":\"x\"}\n"
        );
        assert_eq!(
            argv_of(update.correction),
            strings(&["job", "update", "j1", "--spec-file", "-", "--yes"])
        );
    }

    #[test]
    fn a_global_option_after_cli_is_moved_before_it() {
        let original = strings(&["cli", "--output", "json", "run", "list"]);
        let Some(ServiceCommand::Invalid(invalid)) = misplaced_globals(&original[1..], &original)
        else {
            panic!("invalid expected");
        };
        assert_eq!(invalid.output, Some(OutputMode::Json));
        assert_eq!(
            invalid.correction,
            Some(Correction::Argv {
                action: "Place global options before cli: `{command}`".into(),
                argv: strings(&["--output", "json", "cli", "run", "list"]),
            })
        );
        // Not a service invocation: left to the runner parser.
        let original = strings(&["cli", "--output", "json", "doctor"]);
        assert!(misplaced_globals(&original[1..], &original).is_none());
    }

    #[test]
    fn required_and_extra_arguments_are_rejected() {
        let parsed = invoke(&["program", "delete"]);
        assert_eq!(
            reason(parsed.error.as_ref().unwrap()),
            "missing argument <SLUG> for `cli program delete`"
        );
        let parsed = invoke(&["program", "delete", "a", "b"]);
        assert!(reason(parsed.error.as_ref().unwrap()).starts_with("unexpected argument"));
        let parsed = invoke(&["wallet", "topup"]);
        assert_eq!(
            reason(parsed.error.as_ref().unwrap()),
            "missing required option --amount-cents for `cli wallet topup`"
        );
        // `run download` defaults its directory to ./RUN_ID.
        assert!(invoke(&["run", "download", "run_1"]).error.is_none());
    }

    #[test]
    fn preview_applies_only_to_mutations() {
        let parsed = invoke(&["wallet", "balance", "--preview"]);
        assert!(parsed.error.is_some());
        assert!(
            invoke(&["program", "delete", "x", "--preview"])
                .error
                .is_none()
        );
    }

    #[test]
    fn help_is_the_generated_topic() {
        let ServiceCommand::Help(text) =
            parse(&strings(&["run", "submit", "--help"]), &[]).unwrap()
        else {
            panic!("help expected");
        };
        assert_eq!(text, help_topics()["cli run submit"].as_str().unwrap());
        // --help as an option value is a value, not a help request.
        let parsed = invoke(&["run", "input", "run_1", "--id", "--help"]);
        assert_eq!(parsed.option("--id"), Some("--help"));
    }

    #[test]
    fn public_builds_talk_only_to_production() {
        let production = Environment::production();
        assert_eq!(production.name, "production");
        assert_eq!(
            production.origin,
            "https://run-prose-production.openprose.workers.dev"
        );
        assert_eq!(production.credential_env, "OPENPROSE_API_KEY");
        assert_eq!(production.store_service, "org.openprose.cli.production");
        assert_eq!(production.label(), "OpenProse");
        assert_eq!(production.journal_component(), "production");
        // The endpoint override is read only by a dev-endpoint build.
        let overridden = BTreeMap::from([(
            "OPENPROSE_API_URL".to_owned(),
            "https://example.invalid".to_owned(),
        )]);
        let resolved = Environment::resolve(&overridden).unwrap();
        if cfg!(feature = "dev-endpoint") {
            assert_eq!(resolved.name, "custom");
            assert_eq!(resolved.origin, "https://example.invalid");
        } else {
            assert_eq!(resolved, production);
        }
        assert_eq!(Environment::resolve(&BTreeMap::new()).unwrap(), production);
    }

    #[test]
    fn did_you_mean_is_the_unique_nearest_within_the_manifest_limit() {
        assert_eq!(did_you_mean("wallt", ["wallet", "run"]), Some("wallet"));
        assert_eq!(did_you_mean("xyz", ["wallet", "run"]), None);
        assert_eq!(did_you_mean("ab", ["ac", "ad"]), None);
        // Transpositions are one edit (optimal string alignment).
        assert_eq!(distance("lsit", "list"), 1);
        assert_eq!(did_you_mean("lsit", ["list", "show"]), Some("list"));
        assert_eq!(did_you_mean("--jsno", ["--json", "--yes"]), Some("--json"));
        // Words of up to four characters allow one edit, longer words two.
        assert_eq!(did_you_mean("lst", ["last-x", "list"]), Some("list"));
        assert_eq!(did_you_mean("sbmt", ["submit"]), None);
        assert_eq!(did_you_mean("submt", ["submit"]), Some("submit"));
        assert_eq!(did_you_mean("sbumti", ["submit"]), Some("submit"));
    }

    #[test]
    fn intent_inference_tables_resolve_in_the_manifest() {
        assert_eq!(suggest_verb("ls", &verbs_after(&["run"])), Some("list"));
        assert_eq!(suggest_verb("get", &verbs_after(&["run"])), Some("show"));
        assert_eq!(
            suggest_verb("get", &verbs_after(&["wallet"])),
            Some("balance")
        );
        assert_eq!(
            suggest_verb("rm", &verbs_after(&["org", "member"])),
            Some("remove")
        );
        assert_eq!(suggest_noun_words("organization"), Some(vec!["org"]));
        assert_eq!(suggest_noun_words("whoami"), Some(vec!["auth", "status"]));
        assert_eq!(
            suggest_option("-y", &["--yes", "--json"], |_| true, false, |_| false),
            Some("--yes")
        );
        // `--amount` changes units, so it is a conversion, not an alias.
        assert_eq!(
            suggest_option("--amount", &["--amount-cents"], |_| true, false, |_| false),
            None
        );
        assert_eq!(
            option_conversion("--amount", &["--amount-cents"]),
            Some(("--amount-cents", 100, "dollars", "cents"))
        );
        assert_eq!(option_conversion("--amount", &["--yes"]), None);
        assert_eq!(converted_value("5", 100).as_deref(), Some("500"));
        for value in ["5.50", "0", "-5", "", "1234567890"] {
            assert_eq!(converted_value(value, 100), None, "{value}");
        }
    }

    #[test]
    fn help_words_ask_for_help() {
        let help = |args: &[&str]| help_request(&strings(args));
        assert_eq!(help(&[]), Some(strings(&["--help"])));
        assert_eq!(help(&["help"]), Some(strings(&["--help"])));
        assert_eq!(
            help(&["help", "run", "list"]),
            Some(strings(&["run", "list", "--help"]))
        );
        assert_eq!(help(&["run", "help"]), Some(strings(&["run", "--help"])));
        assert_eq!(help(&["run", "-h"]), Some(strings(&["run", "--help"])));
        assert_eq!(
            help(&["harness", "-h"]),
            Some(strings(&["harness", "--help"]))
        );
        assert_eq!(help(&["run", "show", "RUN", "-h"]), None);
        assert_eq!(help(&["frobnicate", "help"]), None);
    }
}
