# User configuration and exact-command explanation

IMP-097 design, frozen October 6, 2026. This document specifies the candidate
behavior; implementation and qualification are recorded separately in the
workspace task. IMP-097 retains the `openprose` default harness. IMP-098 changes
that built-in to the packaged `agents-sdk` harness after qualification.

## Peer comparison and selected convention

The official [Codex configuration guide](https://learn.chatgpt.com/docs/config-file/config-basic)
uses `~/.codex/config.toml`, project files and omitted values inheriting defaults.
The official [Claude Code settings guide](https://code.claude.com/docs/en/settings)
uses `~/.claude/settings.json`, project settings, per-session overrides and an
explicit configuration-directory override. The official
[OpenCode configuration guide](https://opencode.ai/docs/config/) uses XDG user
settings, project discovery up to the nearest Git directory, and explicit custom
file/directory overrides. Sources were inspected October 6, 2026. The referenced
Prime settings page was unavailable; it supplies no evidence for this decision.

These peers agree on settings independent of installation, separate user and
project layers, and inherited defaults. Home versus XDG locations and environment
precedence vary. The user explicitly chose `~/.prose/cli.toml`. Preserve Prose's
existing flags > environment > project > user > built-ins precedence. Select the
established explicit-directory-override approach as `PROSE_CONFIG_DIR`, governing
ordinary settings only. No extra managed-policy, profile or interactive layer is
introduced by this change. Credential and cache locations are separate concerns.

## Discovery and migration

Normal discovery uses `$HOME/.prose/cli.toml` on macOS/Linux and
`%USERPROFILE%\.prose\cli.toml` on Windows. Existing internal dependency injection
remains available to hermetic tests. `PROSE_CONFIG_DIR`, when supplied, must be a
nonempty absolute directory and selects `<directory>/cli.toml` as the sole user
location. Empty/relative roots fail with their environment source. The override
does not relocate credentials or caches, and disables legacy discovery.

Without that override, consider one legacy location using the former resolver:
`$XDG_CONFIG_HOME/openprose/cli.toml` when the variable is set; otherwise
`~/Library/Application Support/OpenProse/cli.toml` on macOS,
`~/.config/openprose/cli.toml` on Linux or `%APPDATA%\OpenProse\cli.toml` on Windows.
A missing optional legacy root is not an error when canonical discovery works.
An invalid legacy root is reported safely in diagnostics; it must not defeat an
existing canonical file. A canonical/legacy alias of one physical file is loaded
once. Active unreadable, nonregular or malformed settings fail closed.

If only legacy settings exist, load them read-only and emit
`LEGACY_CONFIG_ACTIVE`. Do not copy them during explanation or execution. If both
files exist, canonical settings win as an entire user layer: do not fill omitted
canonical keys from legacy settings. Emit `LEGACY_CONFIG_IGNORED`, explaining
that authority and any differing explicit keys. Invalid ignored legacy content
is a warning containing classification/source only, never its rejected values.
Missing settings use built-ins and do not create directories or files.

`prose cli config migrate [--json]` validates legacy settings and copies their
bytes to canonical settings, including comments, without adding defaults. Secure
atomic creation must refuse existing canonical destinations and unsafe symlinks;
never delete or replace the old file. An existing destination gives
`CONFIG_INVALID` with reason `Canonical user configuration already exists;
migration never overwrites it.` A missing legacy source gives an actionable
configuration error. Root override has no migration source.

`prose cli config unset KEY... [--json]` accepts known TOML keys and removes only
those assignments, preserving other lines and comments. Unknown keys and an
empty key list are invocation errors. With no user file, succeed unchanged and
create nothing. When a legacy file is active, copy its valid explicit settings
into the canonical destination with the requested keys removed, leaving the old
file intact. Reject malformed active files before changing any bytes. Reuse the
existing owner-only, atomic, nonsymlink writer. Removing an override restores
inheritance from the remaining precedence layers and current built-ins.

`prose cli harness use` remains the atomic persistence route for a selected
harness/model/authentication bundle. It saves explicitly supplied choices only.
Other keys can be edited directly in the accepted TOML format. A commented example
is optional; no command materializes inherited defaults automatically.

## Contextual defaults and compatibility

Resolve the harness first, then its built-in model/authentication defaults.

| Harness | Model when omitted | Authentication profile when omitted |
| --- | --- | --- |
| agents-sdk | `gpt-6.1-sol` | `openai-api-key` |
| codex | null: native harness selection | `cached-chatgpt-login` |
| claude | null: native harness selection | `claude-subscription` |
| prime / omp | explicit selection required | explicit selection required |
| openprose / mock | null | null |

Contextual defaults have source `{kind:"default", location:"built-in:HARNESS"}`;
general built-ins retain location `built-in`. Incompatible explicit profiles
fail `CONFIG_INVALID` before executable discovery, credential acquisition or
inference. No account/model/harness fallback is permitted. Explicit lower-layer
model/auth settings belong to the closest harness selection at that layer or
below. If a higher layer selects a different harness, it must replace affected
explicit model/auth settings at that layer or above. Reject stale inherited
bundles rather than silently carrying them over or discarding them. General
built-ins are selected contextually and are not stale explicit overrides.

Keep existing native Codex/Claude model choices and login routes. SDK built-ins
must never become their inherited credential or model choice. Persisting an
alternative and later upgrading the executable preserves the user's settings.

## Pure explanation

Existing syntax remains supported:

```text
prose [runner-global-options...] cli config explain [--json]
```

To inspect the exact planned language invocation:

```text
prose cli config explain [--json] -- [runner-global-options...] COMMAND [ARGS...]
prose cli config explain --json -- --cwd "work space" --harness agents-sdk run snapshot.prose.md
```

Use the execution entrypoint parser and configuration resolver. The target
`--cwd` controls canonical cwd and project discovery; its flags participate in
normal precedence. After the first opaque token or target `--`, preserve every
argument literally, including spellings such as `--model`. The reported target
argv includes the `prose` introducer. Require a target command and reject nested
runner operations, target help/version and target service commands; this surface
explains local language execution. Outer `--json` renders the explanation and
must not become a target output override. Reject runner globals before `cli`
when a separate target vector is supplied, avoiding two contradictory commands.

Explanation performs configuration reads and cwd canonicalization only. It
starts no subprocess, model or agent; inspects no credential stores; and acquires
no published kernel. `cli doctor` and `--dry-run` own readiness. Public profile
names and billing owners are useful diagnostics. Secret values are never printed.

## Frozen machine interface

Retain `openprose.configuration-explanation/1`. New fields are additive for old
fixture compatibility, but every new production report emits all of them.

- `cwd`, `userConfigPath`, `projectConfigPath`, `values`: retain the current
  structure. Emit all nineteen current setting keys. Missing/inapplicable scalar
  values are null; additional-directory/tool lists are empty arrays. Each value
  has its winning `source`. Do not inject these report-only nulls or native
  defaults into validation inputs for other harnesses.
- `target`: null for untargeted operations, otherwise `{argv:["prose",...]}`.
- `locations`: deterministic discovery-order array of
  `{role:"user"|"legacy-user"|"project",path,present,selected}`. Include absent
  considered files and ignored files. List project candidates from target cwd
  upwards through the stopping boundary; no unvisited ancestors are claimed.
- `candidates`: object keyed by setting, with arrays of
  `{value,source:{kind,location},selected}` from lowest to highest precedence.
  File locations contain exact path and line; the owning map key identifies the
  setting. Validate candidate values before displaying them. There is exactly
  one selected candidate for a successfully resolved value. Null/inapplicable
  defaults remain visible. Arrays of candidates are not a second resolver.
- `diagnostics`: deterministic array of `{code,severity:"warning"|"error",
  source,reason}`. Reasons contain safe classifications and known key names,
  never rejected input lines or values. Normal no-config success has `[]`.
- `runtime`: `{transport,permissionMode,authProfile,billingOwner,nativeLimits,
  nativeOutputLimits}`. Unresolved/inapplicable members are null. Resolve
  automatic transport from the existing static capability facts, without probing.
  Report the actual implicit permissions selected by the execution planner;
  unavailable/unenforced permissions must not be represented as containment.
  Installed third-party billing is `user-provider`; native model selection stays
  null when the runner does not select it.

SDK `runtime.nativeLimits` begins with the currently implemented defaults:
`{maxTurns:20,timeoutSeconds:180,toolTimeoutSeconds:30,maxOutputTokens:12000}`.
Native output budgets reuse the execution helper, including fixed record size,
aggregate stdout, capture bound and capture-enabled state. The new harness
packaging work may add bounds through a separately revised shared contract.

A failed explain retains the existing error envelope and nonzero exit. For
configuration errors, `details.configurationExplanation` contains the safely
resolved partial report and the first blocking error in `diagnostics`. Unknown
or unvalidated values must not appear in its candidates/values. No partial
report authorizes execution. Human output displays winning values/sources and
concise migration/error summaries; JSON carries the complete candidate detail.

Migration/unset successful JSON is the same post-operation explanation with
`mutation:{operation:"migrate"|"unset",changed,path,sourcePath,keys}`. `sourcePath`
is the legacy path for migration and legacy-backed unset, otherwise null;
`keys` is the requested TOML key list for unset, otherwise `[]`. Human output
states changed/unchanged, destination and retained legacy source when applicable.

## Acceptance and authority

The shared twelve `operations.config-production-NN` cases reference
`cli/shared/fixtures/config/production-v2.json` for deterministic isolated setup
and byte/absence assertions. The runner prepares files without invoking either
product, executes the actual command, then checks unchanged source bytes,
expected destination bytes, absent paths and redaction sentinels. Both products
are independently compared to these controls. A fake/provider harness start is
always forbidden for these cases, including malformed and stale-bundle cases.

These cases establish basic shared behavior, not the entire release gate.
Independent product tests must additionally cover all setting grammars, malformed
and inaccessible roots, supported Windows home discovery, duplicate keys,
symlink refusal, atomic write failures, repeated/absent unset, multiple-key unset,
legacy-backed unset, unknown keys, invalid/empty target vectors, native-budget
parity and updates to built-ins with explicit overrides retained. Installed-route
and upgrade evidence must verify identical resolution for npm, npx, standalone
and Homebrew. Live SDK fulfillment and packaged-runtime admission belong to
IMP-098 and do not follow from an explanation passing.
