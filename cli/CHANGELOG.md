# OpenProse CLI changelog

This changelog covers the independent CLI implementation under `cli/`. The
repository-level changelog covers the OpenProse language, skill, and plugin
track separately.

Before publishing a CLI release, move its entries into an exact dated version
section named `## [X.Y.Z] — YYYY-MM-DD`. Published changes must not remain
only under `[Unreleased]`; retain that section for subsequent changes. Passing CI and creating
a candidate do not establish public availability or authorize publication.

## [Unreleased]

### Hosted service

- Hold quotes follow the service's per-run holds. `run quote` and the
  `run submit` plan send the given `--model`, `--reasoning-effort` and
  `--environment`, plus `repositories=1` for any `--repo` or
  `--commit-output`; the `job create` plan sends the spec's `model`,
  `reasoning_effort` and `environment`, plus `repositories=1` for a
  `repository_url` or `context_repository_url`. Nothing is defaulted, so a
  flagless quote is unchanged. The `holdBasis` constant, the human quote and
  the help no longer call the hold flat. `run quote --model` with a model the
  service does not offer now returns `SERVICE_REQUEST_REJECTED` instead of
  being ignored.
- Quotes are program-aware: the `run submit` plan posts its exact submission
  body to the quote, `run quote FILE` posts the program and `run quote --from`
  names it, and the `job create` plan names its pinned `program_ref`, so the
  program's own run settings and declared tools count; explicit options
  override them. `run quote` reports the service's `basis` (each value and its
  source). The CLI still never inspects program text.
- `sync_service_interactions.py` accepts the service's public interaction
  projection (`openprose.*-interactions/2`), which carries no principals
  table; the vendored catalog is re-vendored from it.
- `job create` webhook specs accept `model`, `reasoning_effort`,
  `repository_url`, `repository_branch` and `output` for the connected
  contract (the service requires `program_ref` with them).
- `job contract attach` sets a webhook job's run settings: `--model`,
  `--reasoning-effort`, `--repo`, `--commit-output`, `--input` /
  `--inputs-file`, `--environment`, `--file`, `--clear-repo`,
  `--clear-commit-output`, `--clear-input`, `--clear-files` and `--replace`.
  Its plan quotes the hold for the binding as it will run. Re-attaching a
  bound program changes only the options given instead of resetting its
  settings. `--model` is no longer refused. The options are refused for other
  job types.
- An interrupt during a plan's advisory quote (`run submit`, `program draft`,
  `program save`, `job contract attach`) now stops the command instead of being
  swallowed.
- `run quote --help` is reworded more briefly to keep `cli service operations`
  within its size budget.
- `job contract list` reports each binding's saved settings
  (`run_configuration`), `effective_model`, `rev_id`, `bound_at` and
  `is_platform_default`.

## [0.15.0-rc.2] — 2026-10-02

These are accumulated changes in the 0.15 candidate train; some capabilities
also appeared in earlier candidates. This candidate remains explicitly unsigned
on macOS. Its exact bytes require the reviewed native and live qualification
before publication; the version heading does not establish availability.

### npm distribution

- Kernel release candidates use the existing `@openprose/prose-cli` npm identity
  for both the launcher and platform payload versions. Exact optional aliases
  select one binary; no new package bootstrap or lifecycle script is required.
- Publication keeps payload tags separate from launcher channel tags and
  preserves exact-byte integrity checks and historical release plans.

### CI and release path

- Full provider-free source admission on Linux x64 and macOS ARM64, plus
  distribution and unsigned candidate qualification on all four supported
  native targets. Current workflow policy checks locked tools, permissions,
  qualification, retained diagnostics and the protected manual publisher.
- Explicit proxy handling uses the pinned transport in Bun and does not retry
  failed proxy connection attempts. Compiled diagnostics exclude builder paths.
- The installed shared corpus contains 50 cases, including newer Claude telemetry.
  The generic Agents SDK harness has a separate provider-free oracle scenario.
- Unused alpha promotion, frozen alpha registry lineage, historical evidence
  enrichment and the frozen direct legacy-skill experiment are retired. Current
  publication, installed-package custody, schema and privacy checks remain.

### Added

- Claude `print-stream-json` admits any stable release from `2.1.243` up to,
  but not including, `3.0.0` through a new optional recipe field
  `support.minimumVersion`; `admittedVersions` remains the exact audited list
  and other adapters stay exact allowlists. Both parsers now tolerate unknown
  session-bound Claude record types and system subtypes (for example
  `rate_limit_event`, emitted by 2.1.282) as nonterminal telemetry; start,
  terminal, failure, and session-identity checks are unchanged.

- A transport-only functional-alpha package train for standalone Rust,
  standalone Bun, and npm installation on the explicitly admitted POSIX
  platforms.
- Exact installed-harness adapters for the admitted Prime, OMP, Codex, and
  Claude versions, with platform-specific availability and explicit
  authentication boundaries.
- Provider-free package admission and opt-in, cost-acknowledged live transport
  evidence for the nonsemantic `echo-v0` image.
- OpenProse hosted service commands in both products:
  `cli service status|triage|capabilities|operations|guide`, `cli model list`,
  `cli example list|show`, `cli repo list`,
  `cli run quote|submit|watch|input|cancel|list|show|download|share`,
  `cli program list|show|save|visibility|delete|revisions|draft`,
  `cli result list|show|publish|unpublish`, `cli job …`,
  `cli wallet balance|events|usage|redeem|topup` and `cli org …` (no
  organization deletion). One embedded manifest
  (`shared/service/operations.v1.json`, printed by `cli service operations`)
  drives parsing, help, confirmation and transport limits. Every command emits
  versioned JSON or JSONL and never prompts. Commands that spend money, publish
  outward, delete or cannot be undone require `--yes`, and `--preview` shows
  the planned request without sending it. The service commands talk only to
  the production OpenProse service.
- Hosted runs always use a live session that is journaled before submission,
  recover from a duplicate submission, and detach on Ctrl-C or the deadline.
  Only `cli run cancel --yes` cancels. New taxonomy codes include
  `HOSTED_RUN_DETACHED` (exit 21), `HOSTED_RUN_FAILED` and
  `RUN_SUBMISSION_AMBIGUOUS` (exit 22), `HOSTED_RUN_CANCELLED` (exit 24),
  `CONFIRMATION_REQUIRED`, `SERVICE_RESOURCE_NOT_FOUND` and
  `GITHUB_LINK_REQUIRED`.
- `cli service status` reports reachability and the models you can use;
  `cli service triage` reports the service, credential, wallet, organization,
  recent runs and jobs with next commands.
- Customer-facing results carry prices only; human output shows money in
  dollars. Result schemas refuse any property whose name matches `cost`.
- Help and the agent guide: `prose --help` lists the hosted command groups,
  every command and group has `--help` with examples and exit codes (including
  `cli auth`, `cli org list` and `cli package`), and `cli --help` lists the
  everyday groups first. `cli service guide` covers a first program, a daily
  job, scripting, pricing, headless keys and sharing.
- `SERVICE_AUTH_REQUIRED` names `credentialSource`, `credentialVariable` and
  `credentialProblem` in `details`.
- On Linux, the Rust product stores credentials through `secret-tool` in the
  same item the Bun product uses.
- `cli model list` shows each model's status when the service sends a
  `catalog`: the models you can run, premium models that unlock with any wallet
  top-up (with the service's summary and a top-up preview) and older ids still
  accepted with the model to use instead. `--json` adds `result.catalog`
  (`id`, `status`, `tier`, `summary`, `successor` only).
- A premium model refused before a top-up (402 `paid_top_up_required`, or 403
  `paid_model_required` from older servers) is `SERVICE_PREMIUM_MODEL_LOCKED`
  (exit 10) with the model's summary in `details.reason`, a
  `cli wallet topup --amount-cents 500 --preview` Action and
  `details.suggestedArgv`.

### Breaking

- Service environment selection is removed. Every account, organization,
  package and service command reaches the production OpenProse service with
  `OPENPROSE_API_KEY` or the stored production credential.
  - `cli environment show`, `cli environment use` and `cli environment reset`
    are gone and fail with `INVOCATION_INVALID` (exit 2, unknown command).
  - The global `--service-environment` option is gone. Placed before `cli`, it
    fails with `INVOCATION_INVALID` (exit 2) and says the option was removed.
  - `OPENPROSE_STAGING_API_KEY` is no longer read.
  - A saved `service_environment` key in the user `cli.toml` is ignored,
    whatever its value. A user who had selected the alternate environment now
    reaches production; check which credential is in use with
    `prose cli auth status`.
  - The `openprose.service-environment/1` schema
    (`shared/schemas/service-environment.schema.json`) is deleted.
- `cli auth login|status|logout`, `cli org list` and `cli package
  publish|fetch|list|withdraw` print the `openprose.service-operation/1`
  envelope (`schema`, `operation`, `interaction`, `result`, `problem`, with
  keys sorted) like every other service command, instead of the
  `openprose.service-account/1`, `openprose.organization-list/1` and
  `openprose.package-operation/1` documents. `operation` is the dotted
  operation id (`auth.status`, `org.list`, `package.fetch`); the `environment`
  member is gone. The schema files of those names now describe the envelope's
  `result`: `authenticated` and `credentialSource` (whose stored-key value is
  now `store`, not `os-credential-store`), `organizations`, and the package
  receipt, page or withdrawal.
- A malformed API key (from `OPENPROSE_API_KEY` or the store) is
  `SERVICE_AUTH_REQUIRED` with `details.credentialProblem: "malformed"`, not
  `SERVICE_PROTOCOL_INVALID`; the exit code stays 10.
- A registry 404 in `cli package fetch|withdraw|list` is
  `SERVICE_RESOURCE_NOT_FOUND` (exit 10, not retryable) naming the missing
  package version or organization, not the retryable `SERVICE_UNAVAILABLE`.
- An invalid `cli package` reference, slug, version, `--sha256` or `--cursor`
  value is `INVOCATION_INVALID` (exit 2) with the cause in `details.reason`,
  not `CONFIG_INVALID`, and every invalid `cli package` invocation is reported
  in the `openprose.service-operation/1` envelope.
- No npm package or release binary changes until the next published alpha.

### Compatibility and release boundary

- This is a new implementation that reuses the `@openprose/prose-cli` package
  lineage after the historical `@openprose/prose-cli` implementation was
  removed. It does not claim configuration, behavior, or automatic-upgrade
  compatibility with the historical package.
- The `0.15.0-alpha.N` CLI train is independent of the repository's language,
  skill, and plugin versions with the same numeric core.
- The functional alpha transports an opaque task argument vector through the
  selected harness. It does not execute OpenProse semantics or establish
  program portability, semantic conformance, strict containment, provider
  identity, billing identity, or reliability.
- This changelog entry does not claim that an artifact is published and does
  not authorize publication. Package manifests and protected release controls
  remain the authority for any future artifact.
