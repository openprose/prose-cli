# OpenProse CLI implementation status

Updated: 2026-09-01

## Release status

The last authoritative provider-free full local run completed the then-current
39/39 named gates on 2026-08-31, including the then-current 36-case black-box
corpus against both products and the functional-alpha live-runner contract.
The development corpus now contains 48 cases. A fresh real local rehearsal on
2026-09-01 installed its exact internal mock package on all three surfaces and
completed 144 candidate plus 96 differential validations, 240 total. This is
focused evidence for the expanded corpus, not a replacement for a new complete
local admission. After the final portability and evidence changes, exact source
`31d81c55c8c90a7358b1cd8c5a0ccba631290a83` completed the then-current 32/32 quick
aggregate plus the native functional-alpha candidate gates described below.
That retained source result is historical evidence and is not evidence for the
current worktree. All of this is mechanical admission, not semantic or
full-release admission. CI plus
separate
full-release and functional-alpha draft workflows exist, but no artifact
produced by this new CLI implementation has been published. Historical Prose
releases may exist; this status makes no claim about them.

The product has two independent entry paths. An interactive harness TUI loads
the installed `open-prose` skill directly and does not use this CLI. The CLI is
a thin noninteractive wrapper that selects a harness adapter and forwards an
opaque Skill Runtime Image plus opaque task argv. Neither path moves language
semantics or Markdown prompts into the CLI adapters.

Ordinary builds embed `echo-v0`, a release-eligible-for-alpha but deliberately
nonsemantic placeholder. It instructs the selected harness to echo the opaque
task argv and emit an exact closed terminal envelope. It proves wrapper and
adapter transport only; its semantic status is `not-applicable` and it cannot
support a language, portability, Prose Complete, winner, or public performance
claim. The separate `sentinel-transport-test` image remains a provider-free
test fixture and can never enter an alpha or full release.

## Exact functional-alpha candidate evidence

Version `0.15.0-alpha.1` from exact source
`31d81c55c8c90a7358b1cd8c5a0ccba631290a83` was built and packaged twice from
physically distinct clean source roots on both retained
native test hosts. Every byte in each target's closed nine-file inventory was
identical between the two cohorts, including both standalone archives, the npm
meta/platform pair, checksums, dependency evidence, SBOM, provenance, and
release manifest.

| Native target | Pinned build/runtime facts | Provider-free installed admission |
| --- | --- | --- |
| macOS ARM64 | Rust 1.87.0, Bun 1.3.5, Node 24.20.0; Darwin archive structure, minimum-OS claims, and ad-hoc code signatures rechecked with the exact retained system tools | 12/12 journeys: Rust, Bun, and npm surfaces x Prime, OMP, Codex, and Claude |
| Ubuntu 22.04 ARM64 | Rust 1.87.0, Bun 1.3.5, Node 24.20.0; both ELF files stayed within the glibc 2.34 floor and were rechecked with the exact retained `readelf` | 3/3 journeys: Rust, Bun, and npm surfaces through the target-supported Codex adapter |

Each journey installed only the packaged artifacts, selected and persisted a
default harness, ran `cli doctor`, then ran the packaged
`examples/hello.prose.md` without a per-run adapter override. Hostile PATH
shadowing and package/candidate reauthentication remained active. The reports
state `semanticEvaluation: false`, `programPortabilityEvaluation: false`,
`strictDescendantContainment: false`, `releaseEligible: false`, and
`publicationAuthorized: false`. They therefore establish a locally installable,
package-shaped functional-alpha wrapper candidate, not an OpenProse language
release or permission to publish it.

The exact macOS packages also completed a fresh 12/12 live harness observation
in one attempt per cell: Rust, Bun, and npm each passed Prime `0.7.0`, OMP
`18.0.9`, Codex `0.149.0-alpha.4.1`, and Claude `2.1.243`. The closed pathless
aggregate admitted as functional-alpha matrix v4 with SHA-256
`1b02c508225bf0b5ce9ea5a7f5c8549ba7d3ced6bbc7334bbd17cea252d60bd9`.
It is checked in with a scope report and binds the exact candidate and declared
harness/runtime bytes, while the path-bearing records remain private. It
retains no raw provider output or credentials and remains an exploratory transport
observation: `openProseExecuted` is false, provider charging is unverified,
reliability is not measured, and semantic status is `not-applicable`.

## External release gates

- GitHub publication controls are not configured for these workflows. A
  read-only repository check on 2026-08-31 found only the unrelated `release`
  environment, not `openprose-cli-alpha-release`,
  `openprose-cli-profile-admission`, or `openprose-cli-draft-release`; `main`
  had no branch protection and the repository had no rulesets. Those controls,
  exact-tag protection, and an explicit human publication authorization must
  exist before running either draft workflow against a releasable tag.
- A canonical language-owned Skill Runtime Image, task schema, terminal schema,
  semantic corpus/validators, and named minimum semantic release profile.
- OpenProse hosted execution placement, identity, login, billing, quota,
  retention, and spend-control decisions.
- Strict installed-wrapper admission for Prime, OMP, Codex, and Claude.
  Functional-alpha ordinary execution is implemented for all four with no
  fallback, but its prompt/isolation/containment evidence is intentionally
  weaker than strict semantic-release admission.
- A protected validator binding the exact candidate bytes to the canonical
  image, profile, corpus, conformance, benchmark, SBOM, and provenance
  authorities. An image field of `releaseEligible: true` is necessary but not
  sufficient.
- Race-free descendant containment. POSIX evidence settles only the direct
  process, bounded readers, and original group; `setsid()` can escape. Local
  Windows gates have no Job Object authority and refuse runtime evidence.
- Retained execution of the full five-target release workflow. Its
  control-checkout-owned package-admission machinery exists and is designed for
  protected operation, but repository protection is absent and preflight
  correctly blocks `echo-v0` because it is not the canonical language runtime
  and the external authorities are absent, so no full-release reports or draft
  exist.
- Semantic and portability admission of exact release packages. The new
  provider-free eight-case lane proves only release-safe CLI mechanics; it is
  deliberately not the 48-case development corpus or language authority.

The draft workflow references an immutable artifact from an intended future
protected authority producer, but the named producer workflow is intentionally
absent. Candidate-controlled files and dispatch inputs cannot substitute for
that authority.

## Remaining repository release work

- Run both digest-admitted products and their exact packaged sidecar on native
  Windows x64, retain Job Object and installed-launcher evidence, and only then
  authorize a release build with compiled admission `1`. Both integrations and
  packaging are complete provider-free, but current builds intentionally use
  admission `0`.
- Resolve common npm `.cmd`/`.bat` harness shims to adapter-specific direct
  executable/runtime invocations without introducing a shell or generic shim
  interpretation path.
- Run the five-platform workflow from `main` and retain the actual native
  results; a workflow definition is not runtime evidence.
- Add independent license, vulnerability, fetched-package verification,
  signing, conformance, and benchmark authorities to an intended future
  environment-protected aggregate candidate. The deterministic component
  inventory is now package- and
  assembly-bound but cannot supply those authorities.

Until all applicable full-release gates close, the default `openprose` adapter
fails closed with `HOSTED_UNAVAILABLE`. Explicit third-party adapters may run
the functional-alpha echo image, but no such completion is semantic success.
Full-release packaging refuses both the sentinel and `echo-v0`.

## Current local authorities

The products embed and independently parse one deterministic,
language-agnostic bundle. Arbitrary ordered payloads and external
task/terminal/framing artifacts are verified without filename or payload-count
assumptions. Both products expose the same closed machine operations and report
their exact source identity, build profile, and test-seam state.

The provider-free run currently includes:

| Authority | Retained local result |
| --- | --- |
| Shared contracts and image bundle | 20 shared-contract and 14 image tests are green; `echo-v0` is alpha-only and nonsemantic; sentinel remains test-only |
| Independent fake boundaries | Harness and hosted-service fixture suites are green |
| Installed-adapter oracle | 16 oracle tests cover four shared recipes; strict semantic admission remains blocked |
| Adapter product adversary | 17 cross-product adversary tests cover exact shared recipe, wire, terminal, normalized-result, credential-isolation, Prime telemetry control, cleanup parity, OMP runtime admission, and safe parser-diagnostic parity |
| Rust product | 224 workspace tests plus format and strict Clippy are green locally; version/auth/main-run pipe settlement is deadline-bounded even when a `setsid()` descendant retains an inherited descriptor |
| Bun product | 396 tests / 2,473 expects plus type checking and standalone build are green locally; version/auth/main-run readers have the matching bounded retained-pipe behavior |
| Differential products | The last full admission retained 36 cases x 2 products; the current corpus has 48 cases and awaits a fresh complete local admission. The separate installed-package rehearsal passed those 48 cases across all three installed surfaces |
| Conformance process host | 39 runner/signal tests, including bounded capture, explicit sentinel-owned candidate custody, interrupt cleanup, and explicit containment refusal |
| Windows process host | Host-neutral/static/client/integration suites are green; native exact-package runtime execution is unretained and admission remains false |
| Local archives and npm packages | 81 package-local tests cover isolated build/install/integrity/signal/evidence boundaries, the closed ordinary-versus-internal-mock purpose split, exact cohort custody, the Node.js 22.22.3 floor, and versioned-prefix repair |
| Functional-alpha package admission | The complete suite has 22 tests, of which the local runner selects all 11 `AlphaPackageAdmissionTests` class tests, including actual release-profile builds and 12 provider-free journeys: four adapters x the Rust archive, Bun archive, and offline npm-global installation. Each journey binds exact saved configuration, `doctor`, and `run` with no harness or transport override; the OMP fixture carries its own exact Bun 1.3.14 runtime under a hostile PATH; every documented Prime, OMP, and Claude alternative is independently bound to its own doctor/packaged-run journey; `echo-v0` remains nonsemantic |
| Functional-alpha live matrix | 56 live-contract tests enforce the current evidence formats, closed environment custody, checked-in aggregate publication safety, and the closed candidate-reported Prime failure projection. Exact final source `31d81c55c8c90a7358b1cd8c5a0ccba631290a83` passed the checked-in 12-cell Darwin ARM64 matrix: Rust/Bun/npm x Prime/OMP/Codex/Claude, one attempt and one valid exact-route record per cell; matrix SHA-256 `1b02c508225bf0b5ce9ea5a7f5c8549ba7d3ced6bbc7334bbd17cea252d60bd9`. The earlier checked-in cohort remains historical evidence. Evidence v5 and matrix v4 bind target and candidate closure plus bounded declared harness package/runtime byte custody, distribution route, model, and auth-route category; ambient harness configuration, plugins, skills, cached account/provider state, dynamic resources, and provider-side routing remain external and unbound. Provider spend remains unverified, reliability is not measured, and semantic status is `not-applicable` |
| Dependency evidence | 10 tests cover complete package-bound component graphs; independent license, vulnerability, fetched-byte, and signing authority remains absent |
| Windows launch resolution | 13 provider-free tests cover the native-first/direct-runtime oracle; descendant links are rejected and no product admission is granted |
| Installed-package benchmark | 24 tests cover separately admitted ordinary echo/no-seam and internal mock sentinel/test-seam development packages, purpose-separated development/release verification, retained-tree reauthentication, two standalone installs, offline scriptless npm install, three surfaces, exact Node executable/version evidence, strict reanalysis, and Windows pre-spawn refusal |
| Release-package admission | 9 tests install exact release-mode fixture packages and run 8 cases x 3 POSIX surfaces, bind the frozen corpus plus protected/native/package custody, retain structured projections, finally reauthenticate Node/npm executable bytes up to the explicit 256 MiB tool bound, reject stale or fabricated evidence before work or reporting, prove Windows static validation performs no install or spawn, and require a closed canonical failure envelope |
| Local release rehearsal | 16 contract tests plus one fresh real local internal-mock development run; the latter installed three surfaces and completed all 240 selected mechanical validations while every semantic/portability/ranking/release/publication claim remained false. The mock package purpose is unavailable from both packaging CLIs and cannot enter alpha or release mode |
| Benchmark rig | 40 tests cover subject-independent metrics, exact fixture/candidate snapshots, final reauthentication, read-only non-overwriting digest-bound atomic evidence generation, and byte-stable historical reanalysis without a winner |
| Opt-in-lane contracts | The 17-test real-harness contract and 26-test direct-skill contract are host-neutral and green provider-free, with bounded capture, owned-group settlement, closed evidence, read-only non-overwriting digest-bound generation, exact live-input snapshots, and analyzer/report reauthentication; starts do not prove subagent execution and all live observations remain exploratory |
| Release workflow policy | 17 workflow-policy tests are green; the functional-alpha workflow remains manual, draft-only, nonsemantic, and designed for environment-protected operation, while all five full-release v3 package-admission reports stay corpus-, assembly-, and draft-bound; the required repository protection and external semantic/runtime authorities remain absent |

Development packaging snapshots verified executable bytes once before testing
and archiving them. Archives, npm meta/platform tarballs, checksums, narrow
component-inventory SBOM scaffolding, provenance, dependency evidence, and a
release manifest are deterministic. Every
current candidate remains explicitly `releaseEligible: false` and
`publicationAuthorized: false`. The npm launcher performs no install-time or
startup-time download and rehashes its installed closure against the co-installed
manifest before every spawn. This detects local drift but is not independent
signing or provenance authority; the installed package directory remains the
local trust boundary.

Installed npm conformance executes verified launcher snapshot bytes. The
external Node interpreter stays at its resolved installed path so relative
dynamic-library/resource lookup remains valid; its bytes are checked before and
after each case. Its closure is not captured and the final check-to-exec race is
not eliminated. Those limitations, plus POSIX detached-session escape, are
machine-recorded and prevent strict or release admission.

The checked benchmark example retains 80/80 provider-free trials: 10 warmups
and 70 measurements, all settled, with seven eligible success pairs per action
and no exclusions, failures, timeouts, retries, or not-run trials. Cost and
semantic quality are unavailable/not applicable, and no composite winner is
calculated. Historical evidence is bound to its recorded fixture and artifact
digests and is not retroactively promoted to protections that were unobserved.
New collection copies admitted fixture and candidate bytes into private
read-only snapshots, executes only those snapshots, reauthenticates them, and
atomically writes a complete, digest-bound, non-overwriting generation. Subject
stdout has no authority over cost, retry, attempt, or timing-span evidence. The
current live profile requires an intentional refreeze when development artifact
bytes change.

The CLI CI workflow is path-scoped, includes the packaged root license, and defines native jobs for Linux x64 and
ARM64, macOS ARM64 and x64, and Windows x64. Tool runtimes and all external
Actions are immutable pins; every checkout disables persisted credentials. The
separate release workflow is manual-only and draft-only, has a closed
eight-stage build-once graph, and gives repository write permission only to its
draft job, which must not run until its named environment is externally
protected. It requires a `main` control ref, executes preflight and draft code
from a separate control checkout, admits only candidate SHAs that are ancestors
of `main`, and keeps dispatch inputs at quoted boundaries. It names an intended
future protected producer workflow that is intentionally absent, so there is
currently no external artifact capable of satisfying preflight.

The current full-release preflight intentionally fails before compilation
because `echo-v0` is not the canonical language runtime and the canonical-
profile and release-evidence authorities do not exist. It verifies the complete image
directory, embedded bundle, and checksum; later verification binds every
product's reported image digest to that preflight. Code loaded from the `main`
control checkout also
regenerates dependency evidence from the exact candidate checkout and requires
every target package to preserve those bytes. The workflow is configured
to retain failed diagnostics while blocking every downstream job. Final draft
creation uses a closed HTTPS API client with a literal `draft: true`, exact
asset inventory and digest checks, exact archive/npm executable and Windows
sidecar inspection, SBOM/provenance binding, and retained native-build,
preflight, intended protected dependency, and verification lineage. Release notes and
uploads use only the immutable bytes captured during admission, including the
aggregate checksum file itself. Executed native copies are not reuploaded:
later stages independently redownload the original build artifacts and bind
them to the one-file verification reports. The workflow contains no npm/Cargo
publication, tag-creation, push, public release, or promotion step. Its helper
requires an existing version tag that peels to the exact source before POST,
but GitHub provides no atomic tag-SHA precondition for release creation; tag
protection and publication-time revalidation therefore remain external
requirements. Windows Job Object release admission remains false.

After bounded native checks and packaging, the release graph separately
downloads each original package and executes a control-checkout eight-case
release-package invariant lane on all three POSIX surfaces; Windows performs
static full-package validation and blocks before spawn. Assembly and the draft
helper independently rebind the five canonical reports to the original package,
preflight, native lineage, source/control, and workflow identities. No actual
five-platform run has been retained, and the lane explicitly cannot promote
semantic, portability, strict-containment, release, ranking, or publication
claims.

## Historical exploratory model evidence

The sanitized historical real-harness lane contains four direct Prime 0.7 route
observations. Three route canaries passed; one unavailable route retained four
attempts as an internal retry-policy violation. Prime reported $0.00117660 in
metered-equivalent cost; it is not billing-authoritative. Strict-wrapper,
semantic, Prose Complete, and release claims remain false or unknown.

The separate legacy direct-skill proxy contains nine observations. Six created
the exact filesystem effect, but none proved a direct subagent start and none
passed the full old-skill effect criterion. The other observations retained an
output limit, an unavailable route, and a telemetry-policy violation. No
`prose` CLI invocation or interactive TUI was observed. Prime reported
$0.155933308, again non-authoritatively. Current-skill compatibility and
semantic conformance remain unknown; this lane does not replace the language
team's eventual direct interactive regression authority.

## Historical exploratory W54 observations

The historical W54 real-harness sample is mixed. Rust with Prime `0.8.1`
completed `echo-v0` after the exact telemetry-off control was added. Bun with
Prime failed twice with `PROTOCOL_MALFORMED` before `agent_end`. Both Rust and
Bun OMP environment-key runs failed with `PROTOCOL_MALFORMED`. Rust Codex and
Rust Claude completed, with Claude requiring one explicitly authorized retry.
No resolution is inferred for the Prime/Bun or OMP failures. Every observation
is an exploratory, candidate-reported `echo-v0` transport result with semantic
status `not-applicable`; none is final packaged evidence v5/matrix v4,
strict-wrapper or portability evidence, release admission, or publication
authority. The stronger custody formats do not resolve the historical
Prime/Bun or OMP protocol failures.

## Installed harness facts

Provider-free probes preserve the exact recipes, argv boundaries, credential
groups, full image bytes, correlation IDs, and terminal framing for Prime,
OMP, Codex, and Claude. Ordinary release-profile products now execute those
adapters with `echo-v0`; the terminal is deliberately nonsemantic and no strict
wrapper claim is granted.

Functional-alpha admission uses only the exact audited versions Prime `0.7.0`
and `0.8.1`, OMP `18.0.9`, Codex `0.149.0-alpha.4.1`, and Claude `2.1.243`;
nearby patches and prereleases are detected but refused. OMP live evidence used
an exact disposable upstream `18.0.9` installation rather than a user global
install. The OMP recipe and functional-alpha authority carry one structured
runtime prerequisite: Bun `>=1.3.14`, with exact validated combined repair
`npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9`.
Prime/OMP harness-login profiles preserve their HOME-backed stores while
stripping provider credential variables; the wrapper does not claim their
provider, account, billing identity, or auth readiness. Explicit provider-key
profiles instead use a fresh runner-owned mode-0700 configuration directory
through complete child/service settlement, and actual execution is the first
auth authority with no probe or fallback. Every actual Prime child receives
exactly `PRIME_AGENT_TELEMETRY=0`, overriding ambient conflict without changing
credentials; OMP, Codex, and Claude receive no such adapter control.
The first-run selection operation persists one atomic user-scoped
harness/model/auth-profile bundle. Prime and OMP require the latter two
identifiers as explicit CLI selection options; inherited configuration is not
silently promoted into the saved default. Selecting Codex or Claude without
those options removes stale bundle members and retains ADR-0007's frozen
cached-login/subscription defaults. No credential value is written.
Historical live collections predate this repository's current release path.
The source workspace recorded 8/8 direct and later 12/12 installed Darwin ARM64
observations for nonsemantic `echo-v0` candidates. Their private records and
aggregate reports were not carried into this repository. Current admission
uses explicitly synthetic fixtures for capture, custody, matrix and privacy
contracts; it does not claim new live qualification. A future release candidate
needs its own reviewed live collection and canonical language inputs.
