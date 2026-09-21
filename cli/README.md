# OpenProse CLI

This directory contains two independent implementations of the same OpenProse
outer runner: a native Rust binary and a Bun-authored standalone prepared for
npm-compatible installation. Availability is established only by an exact
GitHub prerelease in this repository or an exact version in the public npm
registry; source files and candidate evidence are not availability proof.
Historical Prose releases may exist. The runner selects and supervises an agent
harness; it does not parse OpenProse, own language semantics, or replace the
`open-prose` skill.

There are two deliberately independent ways to run OpenProse:

```text
interactive harness session -> installed open-prose skill -> program

prose CLI -> thin noninteractive harness adapter -> opaque Skill Runtime Image
                                                + opaque task argv
```

The CLI never opens, embeds, or automates a TUI. Conversely, the direct-skill
path does not call or require the CLI. Language-facing Markdown belongs to the
skill and to the language-owned Skill Runtime Image, never to an adapter.

Start with [First five minutes](#first-five-minutes). Review the independent
[CLI changelog](CHANGELOG.md) for version and compatibility boundaries. For
installation or CLI problems, use [Support and report a CLI problem](SUPPORT.md).
Release operators use the
[functional-alpha readiness contract](release/ALPHA_READINESS.md) to distinguish
candidate, promotion, and post-publication authority.

For the candidate service-account connection, see [Connect the CLI to staging](../docs/staging-account.md). This does not enable registry publishing or hosted execution.

## First five minutes

1. Read [Install the functional alpha](#install-the-functional-alpha) and verify
   the external availability authority before using an installation command.
2. When an authorized alpha is available, install one exact package surface and
   use only its exact executable and packaged README.
3. Follow [Try the functional-alpha transport
   smoke](#try-the-functional-alpha-transport-smoke) for the shortest admitted
   first run. Read the provider-charge boundary before executing its run
   command.
4. Use [Support and report a CLI problem](SUPPORT.md) for installation or
   existing-CLI problems. Harness or model-route proposals and benchmark
   proposals use the two separate routes linked there.

## Install the functional alpha

Do not infer availability from this source tree. A GitHub prerelease in this
repository that lists the exact artifact and `SHA256SUMS` establishes the
standalone route. The public npm registry's exact version and `alpha` dist-tag
establish the registry routes. Verify the relevant external authority before
running an installation command.

When an authorized functional alpha is available, choose one route:

- From its GitHub Release, download `SHA256SUMS` and the Rust or Bun standalone
  archive for your platform. Verify the archive against `SHA256SUMS` before you
  extract it, then follow the archive's packaged `README.txt`.
- From npm, use Node.js 22.22.3 or newer. The `alpha` distribution tag is the
  convenient channel; an exact published prerelease version is the reproducible
  route. Do not run either installation unless the registry establishes it.

To follow an available moving alpha channel, resolve its exact version once,
validate that registry value, and install that exact package into the same
versioned prefix documented inside the package:

```sh
ALPHA_VERSION=$(npm view --silent '@openprose/prose-cli@alpha' version) || \
  { echo 'could not resolve functional-alpha version' >&2; exit 1; }
case "$ALPHA_VERSION" in
  *[!0-9A-Za-z.-]*) echo 'invalid functional-alpha version' >&2; exit 1 ;;
esac
printf '%s\n' "$ALPHA_VERSION" | \
  grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-alpha\.(0|[1-9][0-9]*)$' || \
  { echo 'invalid functional-alpha version' >&2; exit 1; }
ALPHA_PREFIX="$HOME/.local/openprose-cli-$ALPHA_VERSION"
npm install --global --ignore-scripts \
  --prefix "$ALPHA_PREFIX" \
  "@openprose/prose-cli@$ALPHA_VERSION"
```

For a reproducible installation, enter an exact version shown in the published
release. The command stops before npm unless the value has the complete
`X.Y.Z-alpha.N` form:

```sh
printf '%s' 'Exact published functional-alpha version: '
IFS= read -r ALPHA_VERSION
printf '%s\n' "$ALPHA_VERSION" | \
  grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-alpha\.(0|[1-9][0-9]*)$' || \
  { echo 'invalid functional-alpha version' >&2; exit 1; }
ALPHA_PREFIX="$HOME/.local/openprose-cli-$ALPHA_VERSION"
npm install --global --ignore-scripts \
  --prefix "$ALPHA_PREFIX" \
  "@openprose/prose-cli@$ALPHA_VERSION"
```

The npm package selects its platform package and fails closed on an unsupported
platform. Each npm block leaves `ALPHA_PREFIX` set to the installation it just
created; keep that value in the same shell for the first-run block below. Use
the exact installed executable and the packaged README for later repair,
upgrade, and removal. Source builds and unpublished package rehearsal remain
documented in [`release/README.md`](release/README.md).

## Current status

Both runners now have a provider-free, mechanically admitted functional-alpha
path for four user-installed harness adapters: Claude Code, Codex CLI, Prime
Agent, and OMP. The committed `echo-v0` Skill Runtime Image is intentionally
simple: it asks the harness to echo the exact task argv and return a closed
terminal envelope. The provider-free proof uses a frozen fake harness and
establishes wrapper, discovery, prompt delivery, task delivery, and protocol
recovery mechanics; it is not a claim that every real-provider cell passes and
it does **not** parse or execute OpenProse yet.

The image is marked `releaseEligible: true` only for the explicitly nonsemantic
functional alpha. Every generated package manifest still records top-level
`releaseEligible: false` and `publicationAuthorized: false`. A full semantic
release continues to require the canonical language-owned image.

Those boundaries are intentionally narrower than strict process isolation. On
POSIX, supervision can settle the direct process, both bounded output readers,
and the original process group, but a hostile descendant can escape that group
with `setsid()`. On Windows, local Python gates refuse runtime measurement
before spawn because a process group is not Job Object authority. Neither
platform currently supplies strict release containment through this CLI tree.

The functional-alpha GitHub workflow is defined and policy-tested to build
release-profile Rust and Bun artifacts for four POSIX targets, package the npm
meta/platform pair, verify the exact downloaded packages, and prepare a draft
prerelease for separate protected promotion. Required environment, tag, and
`main` protections are external repository settings and must be verified before
dispatch; the checked-in workflow does not establish them. Candidate or draft
creation does not establish public availability. A full semantic release remains
blocked on the canonical image, semantic corpus/profile, OpenProse-hosted
billing, native Windows evidence, strict containment, and protected release
authority.

Linux artifacts declare a fixed glibc 2.34 minimum and their measured ELF
requirements. They currently carry execution evidence for Ubuntu 22.04 only;
other Linux environments remain unverified, and the npm launcher refuses a
detected glibc below that floor before spawning the packaged executable.

The default OpenProse-billed route remains unavailable and fails closed. The
four BYO adapters execute only when explicitly selected and never silently
change harness, wrapper-selected credential group, or billing owner. Harness-
internal account/provider routing remains harness-managed. See
[`protocol/STATUS.md`](protocol/STATUS.md) for the evidence and open gates.

The exact packaged candidate from source commit
`31d81c55c8c90a7358b1cd8c5a0ccba631290a83` completed the current
evidence-v5/matrix-v4 12-cell Darwin ARM64 collection on 2026-08-31: Rust,
Bun, and npm each passed Prime, OMP, Codex, and Claude. The unchanged pathless
aggregate and its limitations are retained in the
[`live-alpha` evidence report](conformance/live-alpha/evidence/31d81c55c8c90a7358b1cd8c5a0ccba631290a83/REPORT.md).
Earlier direct 8/8 and evidence-v2 12/12 collections remain historical and
cannot be relabeled into the current formats.

Matrix v4 binds the target and candidate closure plus each harness's bounded
declared package/runtime byte closure, distribution route, model, and auth-route
category. Ambient harness configuration, plugins, skills, cached account and
provider state, dynamic resources, and provider-side routing remain explicitly
external and unbound. The valid exact-route collection contains one passing
record per cell, not a reliability sample. Provider spend is unverified,
semantic status is `not-applicable`, and `echo-v0` remains deliberately
nonsemantic. The matrix does not establish OpenProse execution, portability,
strict wrapper admission, release eligibility, or publication authority.

A separate historical exploratory W54 run produced mixed real-harness results.
Rust with Prime `0.8.1` completed `echo-v0` after the telemetry-off control was
added. Bun with Prime failed twice with `PROTOCOL_MALFORMED` before
`agent_end`. Both the Rust and Bun OMP environment-key runs failed with
`PROTOCOL_MALFORMED`. Rust Codex and Rust Claude completed; Claude required one
explicitly authorized retry. These observations are exploratory, nonsemantic
`echo-v0` transport results, not a resolution of the malformed-protocol
failures and not final packaged evidence-v5/matrix-v4 results. Stronger custody
formats do not resolve historical protocol behavior.

## Try the functional-alpha transport smoke

After installing the npm package, keep the `ALPHA_PREFIX` set by the selected
installation block above. Use it to select the exact executable and packaged
example; do not rely on an ambient `prose` executable with the same name:

The run command contacts the selected provider and may incur charges under the
signed-in account. The CLI cannot determine the account or billing route.

```sh
: "${ALPHA_PREFIX:?run one npm installation block above in this shell}"
PROSE="$ALPHA_PREFIX/bin/prose"
EXAMPLE="$ALPHA_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
npm install --global @openai/codex@0.149.0-alpha.4.1
codex login
"$PROSE" cli harness list
"$PROSE" cli harness use codex
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

For a standalone archive, follow its packaged `README.txt`; that route does not
use `ALPHA_PREFIX`. Set `PROSE` and `EXAMPLE` to the exact extracted `.../prose`
and sibling `.../examples/hello.prose.md` paths before using the remaining
harness-selection guidance below.

Harness support is platform-specific in this functional alpha:

| Platform | Supported harnesses |
| --- | --- |
| macOS Apple silicon | Prime Agent, OMP, Codex CLI, Claude Code |
| macOS Intel | Codex CLI |
| Linux x64 with glibc 2.34 or newer | Codex CLI, OMP |
| Linux ARM64 with glibc 2.34 or newer | Codex CLI |
| Windows, Linux with musl, and other platforms | Not supported |

Choose only a harness listed for your platform. The CLI fails closed rather
than using another harness. To use another admitted harness, replace `codex`
with its CLI identifier: `claude`, `prime`, or `omp`. Codex CLI and Claude Code
use their own installed login or subscription state. Run `codex login` for
Codex or `claude auth login` for Claude before selecting it. For Prime Agent,
start the installed `prime-agent` harness separately, complete its own
interactive sign-in or configuration, and then exit it. For OMP, start the
installed `omp` harness separately, complete its own interactive sign-in or
configuration, and then exit it. OpenProse CLI never invokes or controls that
TUI. Prime Agent and OMP require an explicit model and authentication route. To
use the login already managed by the installed Prime Agent harness:

```sh
"$PROSE" cli harness use prime \
  --model openai-codex/gpt-5.4 \
  --auth-profile prime-harness-login
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

The model is illustrative: replace it with a fully qualified
`provider/model` exposed by the login you intend to use. Use
`omp-harness-login` for OMP. These explicit routes preserve the harness's
HOME-backed login store but strip provider credential environment variables.
Their readiness, provider, account, and billing identity remain unknown to the
wrapper; the profile names are not subscription-billing claims. Explicit
provider-key profiles such as `openai` remain separate, require the matching
environment credential, and run with a fresh runner-owned mode-0700 harness
configuration directory for the complete child/service lifetime so a cached
login cannot silently take precedence. Prime/OMP readiness remains `unknown`
until the actual run; `doctor` does not start an authentication probe or fall
back to another route. Every actual Prime child receives the adapter-owned
`PRIME_AGENT_TELEMETRY=0` opt-out, overriding an ambient conflict without
changing credentials. OMP, Codex, and Claude never receive that control.

`cli harness use` saves one coherent user-scoped selection. For Prime and OMP,
the model and auth profile must be explicit on that command; inherited project,
environment, or older user-config values do not count as consent to persist
them. Switching to Codex or Claude without selection options atomically clears
any stale saved model/profile and uses the frozen cached-login/subscription
route. The saved file contains identifiers only, never credential values.
It is the active default when no higher-precedence project or environment
setting overrides it; a conflicting active harness setting is rejected during
selection instead of reporting a misleading switch.

`PROSE_MODEL` and `PROSE_AUTH_PROFILE` are equivalent environment-based
configuration for individual doctor/run invocations; they are not implicitly
copied into `cli harness use`. The CLI forwards only the selected adapter's
allowlisted credential environment group and never places credentials in argv
or evidence.
Functional-alpha admission is an exact allowlist: Prime `0.7.0` and `0.8.1`,
OMP `18.0.9`, Codex `0.149.0-alpha.4.1`, and Claude `2.1.243`. The OMP
authority targets the upstream `@oh-my-pi/pi-coding-agent@18.0.9` package and
its `omp` executable. OMP `18.0.9` has the structured runtime prerequisite Bun
`>=1.3.14`; the exact validated combined repair is
`npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9`. Nearby
patches and prereleases are detected but are not inferred compatible.

With `echo-v0`, a successful run proves nonsemantic transport completion. The
runner does not open or read the packaged example file: its path is only an
opaque task argument, so the placeholder cannot print the program's
`Hello, world!` return value. Human streaming withholds control-shaped JSON and
content that reproduces task, model, path, or credential values, while JSON and
JSONL retain their settled machine schemas. Swapping in the canonical image
must not require adapter changes.

## Build and try both candidates locally

The workspaces are isolated from the repository language packages.

For the shortest provider-free development loop from the repository root, use
the local driver. It deliberately builds the separate sentinel/test-seam
profile, removes provider credentials and ambient build overrides, hashes the
exact outputs, and runs one language-agnostic transport smoke test:

```sh
python3 cli/ci/build_local.py --smoke --json
```

It does not install globally or modify user configuration. An explicit,
previously nonexistent disposable install directory can be populated with
`--install-dir PATH`. When `--package PATH` is present, the driver never sends
those sentinel/test-seam bytes to the packager. It builds and snapshots a
second ordinary development pair with `echo-v0` and test seams disabled, then
packages only that pair. The JSON report records the smoke candidates and the
separate package-input identities. See
[`release/README.md`](release/README.md#development-package) for the exact
command and lower-level equivalent.

Ordinary repository builds default to `echo-v0`. An alpha package build uses
Rust `--release`, Bun's release build, and the same exact prerelease version and
source identity through `OPENPROSE_BUILD_VERSION` and
`OPENPROSE_BUILD_COMMIT`. See [`release/README.md`](release/README.md) for the
complete local build/package/install commands.

The explicit `mock` remains a deterministic provider-free fixture, never an
agent or fallback. It exists only in test-seam builds made with the sentinel;
release-profile and ordinary `echo-v0` builds refuse it. Ordinary language
arguments remain opaque after the first language-command token. Runner-owned
automation uses the exact CLI executable selected for the run:

```sh
PROSE=/absolute/path/to/prose
"$PROSE" --output json cli config explain
"$PROSE" --output json cli harness list
"$PROSE" --output json cli doctor
"$PROSE" --output json cli auth status
```

`doctor` and unavailable account operations intentionally return the selected
problem's nonzero exit code while emitting a schema-valid JSON report.

## Test authority

Install the pinned Python test dependencies, then run the provider-free local
admission command from the repository root:

```sh
python3 -m pip install -r cli/ci/requirements-test.txt
python3 cli/ci/run_local.py
```

The full command currently runs 42 fail-fast gates: shared/image contracts,
independent harness and hosted fixtures, Rust and Bun suites, differential
black-box behavior, process settlement, architecture/dependency boundaries,
deterministic dependency inventory and release-note evidence, Windows
resolution plus static/host-neutral checks, isolated archive/npm installation,
exact functional-alpha package admission, and the benchmark and release-
workflow contracts. Passing them is local mechanical admission only; it cannot
grant semantic, native-Windows, strict semantic-adapter, or public-release
eligibility.

The retained provider-free local run completed the then-current 39/39 gates on
2026-08-31. That result includes the nonsemantic functional-alpha package
proof: Prime, OMP, Codex, and Claude each completed exact saved configuration
to `doctor` to `run` journeys through the Rust archive, Bun archive, and
offline npm-global installation, with no harness or transport override on the
run. It remains mechanical evidence only.

The control-checkout release graph, designed for protected operation, now
redownloads the original per-target package artifacts and runs a provider-free
release-package admission lane before assembly. On POSIX it installs both standalone archives plus the
offline npm meta/platform pair and executes eight release-safe invariants on
all three surfaces. On Windows it validates the complete static
archive/npm/sidecar lineage and refuses candidate execution before spawn. Each
`openprose.release-package-admission/3` report binds the raw frozen corpus and
retains the expected structured projection for every observation. It is
independently rebound to the original
package and native bytes during assembly, and records the exact, finally
reauthenticated Node/npm executable identities used for POSIX installation. The
draft helper validates
that closed evidence again before
any GitHub API call. Provider calls are reported as `not-observed`, since this
boundary has neither network-isolation nor billing authority. This lane is
not the 48-case development corpus, a language/semantic portability test,
strict-containment evidence, or publication authority; the workflow has not
yet produced retained five-platform evidence because full-release preflight
correctly refuses the noncanonical `echo-v0` image and absent protected
authorities.

The Windows process host now has coherent bounded wire contracts, independent
Rust and Bun integrations, authenticated exact-sibling discovery, and
digest-bound sidecar packaging. Current builds deliberately compile admission
off, so Windows process runs still fail closed until native Windows runtime
evidence exists and release authority intentionally changes the gate. Common
npm `.cmd`/`.bat` harness shims also need an adapter-specific direct-runtime
resolution design; they will never be launched through a shell fallback.

Use `--list`, repeatable `--only GATE`, or `--quick` while iterating. Real model
and direct-skill executions are separate, opt-in, cost-bounded lanes and never
run as a side effect of local tests; only their provider-free runner contracts
are part of ordinary admission.

The benchmark rig records complete randomized blocks, retains failures and
timeouts, calculates latency only from successful trials, pairs only two
successful observations on the same surface, and never emits a composite
winner. Sentinel evidence cannot make semantic, portability, or public
performance claims.

Checked benchmark evidence is digest-bound historical evidence with a
non-overwriting generation contract. New collection admits the live inputs,
copies them into private read-only snapshots, executes only those snapshots,
reauthenticates them before reporting, and atomically writes one complete,
digest-bound generation. Target stdout cannot author cost, retry, attempt, or
timing-span evidence. Snapshot custody detects ordinary same-user mutation but
is not described as a privilege boundary.
The installed-package benchmark separately extracts both standalone archives,
installs the npm meta/platform pair offline, and measures all three launch
surfaces while keeping install cost distinct from invocation time. Run
`python3 cli/ci/rehearse_release.py --output PATH` for a build-once,
package-once, checksummed local rehearsal; it remains provider-free and
explicitly non-releasing. That rehearsal alone requests an internal
`mock-benchmark` package purpose, which packages the same sentinel/test-seam
snapshots that passed its smoke step. Neither `build_local.py` nor
`package_local.py` exposes that purpose as a command-line option; ordinary
`build_local.py --package` remains the separate `echo-v0`/no-seam route above,
and alpha/release packaging refuses the mock purpose. A real one-trial local
rehearsal has successfully installed all three mock development surfaces and
completed all 240 selected mechanical validations. That is useful package/DX
evidence, not semantic, portability, strict-containment, release, or
publication evidence.

## Package locally

The local packager creates deterministic Rust/Bun archives plus npm meta and
platform tarballs, then installs and tests the captured bytes without a
registry, postinstall script, or runtime download. It snapshots each input once
before verification so a concurrent mutation cannot change the package after
admission. See [`release/README.md`](release/README.md) for the exact command and
offline two-tarball npm install.

Local packager output is candidate-only, including in alpha or release mode:
the manifest always says `releaseEligible: false` and
`publicationAuthorized: false`. Development mode is additionally restricted to
local test artifacts. The narrow SBOM and provenance files are scaffolding, not
a protected release attestation. A
checksummed deterministic inventory covers the resolved Rust CLI, Windows
process-host, and Bun dependency graphs and is bound into the manifest, SBOM,
provenance, and draft assembly; license, vulnerability, fetched-package, and
signing authority remain explicitly unresolved.

## Directory guide

- `shared/`: closed schemas, fixtures, capability facts, and the opaque image.
- `conformance/`: shared black-box, fake-harness, hosted, and opt-in real lanes.
- `rust/`: standalone native implementation.
- `bun/`: standalone Bun implementation and npm distribution source.
- `benchmarks/`: externally owned black-box measurement and scorecards.
- `ci/` and `release/`: local admission, exact-byte candidate packaging, and
  fail-closed release-policy foundations. A functional-alpha workflow can make
  a draft prerelease from `echo-v0`; the separate full-release workflow refuses
  that noncanonical image and cannot obtain its intentionally absent protected
  producer artifact before any build or external change.
- `labs/`: research-only SDK/embedded-agent investigations.
- `platform/`: platform-specific supervision research and helpers.
- `protocol/`: decisions, path ownership, and evidence status.

[`SPEC.md`](SPEC.md) is the complete architecture, conformance, benchmark, and
release contract. [Contributing to the CLI](CONTRIBUTING.md) explains the
human and agent development workflow and the request process for a new harness,
model route, or benchmark cell.
