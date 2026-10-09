# CLI distribution (IMP-014)

Current status (October 5, 2026): unsigned `0.15.0-rc.2` is published through
npm, GitHub and pkg.prose.md. Homebrew tap preparation is in progress with
user-selected formula names `prose-bun` and `prose-rust`, both providing the
command `prose`. Neither is designated the default.
[Publication setup](cli-publication.md) owns signing/publication policy;
[next-candidate preparation](cli-release-next.md) owns current runtime gaps and
release gates.

## Homebrew installation and implementation selection

The selected tap is `openprose/tap`. Each formula selects a verified immutable
release; the current candidate version is `0.15.0-rc.2`. RC status appears in
its version and installation caveats, rather than a package-name suffix.
Both implementations are prebuilt; no Bun or Rust compiler is required.

After the tap installation checks pass, select one implementation:

```sh
brew install openprose/tap/prose-bun
# Or select Rust instead:
brew install openprose/tap/prose-rust
prose --version
```

Both packages provide `prose`. To switch from Bun to Rust without overwriting
the active command:

```sh
brew unlink prose-bun
brew install openprose/tap/prose-rust
# If Rust is already installed, use: brew link prose-rust
prose --version
```

Switch back with `brew unlink prose-rust` and `brew link prose-bun`. Homebrew
retains both installed kegs, with one linked command. An attempted simultaneous
link refuses to overwrite the existing `prose`; do not use `--overwrite`.
An existing npm installation may precede Homebrew in PATH. Check the actual
executable and version rather than assuming the selected package wins PATH.

These are experimental prereleases. macOS binaries are not Developer ID signed
or notarized; Linux requires glibc 2.34 or newer. Install and authenticate a
supported harness separately. A CLI executable does not pin the selected
kernel or harness. Homebrew packaging does not qualify program fulfillment.
The existing public RC2 remains behind main's merged Prime fix and does not
claim the separately observed cleanup failure repaired.

No plain `prose` package alias is selected until the implementation default is
decided. Homebrew needs no separate publisher account; the tap uses the existing
GitHub organization. Future formula updates select a newly qualified public
release and pass installation/switching tests before merge. CLI distribution CI
first tests Homebrew against this checkout's verified development-rehearsal
archives on all four native runners. These local-only bytes never become tap
releases. The unsigned candidate build also runs the same checks against its
already-built kernel-RC archives, binding the expected source/version, native
custody reports and executable hashes. A failed platform withholds the candidate
cohort; neither packaging check grants live qualification. The tap update workflow follows the guarded public RC pointer,
verifies the immutable manifest digest and prepares a formula update branch.
Its own-repository CI is explicitly dispatched for bot-created branches. The
release owner opens the checked PR using existing GitHub authentication,
respecting the organization policy against Actions-created PRs. Merging that
checked PR selects the new Homebrew release. This avoids a separate
cross-repository publishing token and preserves a reviewable update boundary.

## Packaged Agents SDK candidate

The IMP-098 candidate supplies the Agents SDK helper with each native CLI package.
Its built-in selection is `agents-sdk`, `gpt-6.1-sol` and `openai-api-key`;
a supported first run requires an `OPENAI_API_KEY`. Saved alternatives and explicit
command overrides remain available. Use `prose cli config explain --json -- run
input.prose.md` to inspect the selected settings and their sources.

On macOS, the helper requires its sibling `prose-agents-sdk-runtime` directory.
Standalone relocation must move the entire extracted package together. npm and
Homebrew install the helper and complete support directory with the selected CLI.
Linux retains the packaged single-file helper. End users do not provision Python
or a separate SDK executable. Missing prerequisites fail explicitly.

These are candidate behaviors. Complete source admission, native installation and
upgrade checks on all supported platforms, and live fulfillment evidence must
qualify the final source before release. Existing public releases retain their
historical behavior and qualification.

## Historical implementation record — September 16–17, 2026

The sections below retain earlier work, failures and decisions. Statements about
unmerged PRs, missing workflows, no public release and signing/name proposals
describe those dates; current publication policy above supersedes them.

This branch is independent of IMP-008 startup changes. It adds a provider-free
Actions rehearsal for the existing packager and an adapter that exports verified
package bytes to the distribution repository's reviewed-plan format. It does
not publish npm, create GitHub releases, deploy the endpoint or change kernel
startup. Dependency locks and IMP-008 startup behavior are unchanged. Rehearsal uncovered
pre-existing fake-transport diagnostic drift and a bounded reader-settlement
timing problem; the focused fixes and their shared cases are included here.

## Installation strategy and naming

The user confirmed `@openprose/prose-cli` as the public npm identity on
September 17, 2026 (UTC), correcting the earlier proposed `@openprose/prose`
name. Keep the existing package and publish a new version; never overwrite an
existing version. The optional alternative-name development rehearsal remains
historical test tooling and is not a public migration plan. See
[Publication and signing](cli-publication.md) for the exact OIDC identity and
release gates.

Standalone Bun and Rust downloads remain available independently. The historical
Rust-default and `prose-bun` alternate-command proposal below is superseded by
the October 5 user-selected implementation package names and common `prose`
command above. Existing stable-only tooling remains the default, with explicit
RC version/qualification admission added for the new tap.

## Local build and installation rehearsal

Install Rust 1.87.0, Bun 1.3.5, Node 24.20.0 and Python 3.10.20. Install the hashed
Python requirements in a private virtual environment, and the frozen Bun lock:

```sh
python3.10 -m venv .venv-distribution
. .venv-distribution/bin/activate
python3 -m pip install --require-hashes --only-binary=:all: -r cli/ci/requirements-test.txt
(cd cli/bun && bun install --frozen-lockfile --ignore-scripts)
python3 -m unittest discover -s cli/ci -p test_distribution_plan.py
python3 cli/ci/rehearse_release.py --output /tmp/prose-rehearsal-NEW --trials 1
```

Use a fresh output directory every time. The existing rehearsal builds both
implementations, packages them, extracts standalone installs, installs npm
tarballs offline without lifecycle scripts, and performs deterministic mock
checks. It uses development test seams and the sentinel; these artifacts must
never be released publicly. It makes no provider calls and leaves local build
outputs plus the requested disposable evidence directory. It does not establish
kernel semantics, supported-platform qualification or release readiness.

The Actions workflow `.github/workflows/cli-distribution-check.yml` runs this
rehearsal on macOS arm64/x64 and Linux arm64/x64. These are proposed test lanes,
not claims of success before execution. No publication secrets are provided.
Windows is excluded pending its separate native containment qualification.
Existing documentation references other release workflows missing from this
repository; this focused rehearsal does not claim to restore those authorities.

## Hosting boundary

For an already-qualified package directory, `cli/ci/distribution_plan.py`
checks every artifact against its package manifest and binds the original
manifest, checksums, SBOM, dependency evidence and provenance into a hosting
plan. It never rebuilds, renames versions or grants qualification. It requires
an immutable evidence URL and writes a fresh local plan for review. Alpha
versions are rejected rather than silently renamed into the new `dev`/`rc`
channels. Reconciling the earlier alpha train is a release decision.

The distribution publisher fetches exact reviewed GitHub release assets,
checks source/tag and artifact digests, and mirrors them under
`/cli/releases/VERSION/`. Channel documents have separate `dev`, `rc`, and
`stable` pointers. Reviewed kernel qualification is required before any public
staging. The kernel's `/kernel.md` pointer is unchanged. Neither endpoint
checksums nor a pinned CLI guarantee a pinned kernel; record both selections.

## Observed blockers and validation

Local export tests pass (four tests), and npm identity tests pass (three tests,
including an offline installation and failure exit propagation). Actions syntax
passes actionlint 1.7.12.
The initial rehearsal stopped because the active Python interpreter lacked the
pinned contract dependencies. Using the prepared virtual environment proceeded
through builds and then failed at packaging because root `LICENSE` is absent.
Rust metadata and npm package generation declare MIT; the kernel repository
already carries MIT with Copyright (c) 2026 OpenProse. The user subsequently approved MIT; the standard license text and existing
2026 OpenProse copyright notice are now included at repository root.
The subsequent rehearsal found a Python 3.9 `Path.write_text` incompatibility;
Python 3.11 was also incompatible with the retained wheel-hash set. Python
3.10.20 matches the existing pinned dependency set and is now the workflow pin.

The next complete rehearsal reached cross-product checks and exposed missing
fake-transport diagnostics in Bun and missing byte counts in Rust. Shared
cases now require the same closed reasons, admitted-record counts and bounded
byte counts. Those targeted changes let the three installed surfaces pass all
144 candidate/case validations in the local one-trial rehearsal. This remains
development sentinel evidence, not a kernel or release qualification.

The Rust supervisor's existing backpressure test failed twice: sleeping one
millisecond after empty polls could exhaust its 250 ms drain deadline. Waiting
for a channel message within the same deadline fixes that behavior without
extending the production timeout or reducing assertions. Retain the failures
alongside the corrected test result.

Before public release: verify existing package ownership and version lineage,
restore or replace missing release/promotion authorities, qualify the actual
IMP-008 artifact set, pass native installation lanes, label the owner-authorized unsigned RC explicitly and track subsequent Apple
signing in IMP-015, verify dependency notices, and configure trusted publication.
No model runs, registry changes or global Prose installations occurred in this work.
Build/test toolchains were prepared separately in temporary directories.


## Current local handoff

The retained validation logs for this work were removed from the repository
because they recorded local machine paths. The complete local rehearsal passes
on macOS arm64. `rehearse_npm_identity.py PACKAGE --out FRESH`
then repackages the verified Bun archive under the preferred npm identity,
installs the exact local tarball pair with scripts and registry access disabled,
and verifies the launched version. The Actions workflow includes that step.
No global CLI installation is required.

The candidates are now published in CLI PR 2 and distribution PR 1. The first
remote rehearsal exposed two fresh-runner setup defects: setup-python did not
provide Python 3.10.20 for macOS ARM, and the offline Rust build lacked downloaded
Cargo dependencies on the other three platforms. The workflow now obtains the
same pinned Python through pinned uv and explicitly fetches the locked Cargo
dependencies before retaining offline build behavior. Remote rerun results remain
a separate qualification step. The user authorized merge and publication on
September 16, 2026; actual release artifacts still require the stated gates.


## Remote qualification and publication gates (2026-09-17 UTC)

Distribution PR 1 merged at `691dc805`; its endpoint deployment succeeded in
[run 35168653526](https://github.com/openprose/openprose-distribution/actions/runs/35168653526).
A read-only check confirmed the existing root redirect still serves the kernel
catalog. `/cli/channels/stable.json` returns 404 because no qualified CLI release
has been staged. This is deployment of hosting support, not publication of a CLI.

CLI [run 35168634350](https://github.com/openprose/prose-cli/actions/runs/35168634350)
passed on macOS ARM64 but failed elsewhere. The x64 Bun builds failed inside isolated compilation. Commit `2549a78`
installs the matching pinned baseline runtime during setup to avoid a runtime
download after network access is disabled. Linux ARM64 compiled and
packaged, then failed the frozen adapter corpus because Claude and Prime admit
only macOS ARM64; OMP admits macOS ARM64 and Linux x64. The generic 48-case corpus
assumes those adapters can start on every POSIX host. Rust and Bun also use
different native architecture strings in rejected-host diagnostic details.

Do not expand adapter admission or remove a failing platform to green the gate.
A subsequent change must specify shared host-aware expectations, verify the
required rejection on unsupported hosts, and distinguish successful native
packaging from adapter conformance. Until then, CLI PR 2 must remain unmerged if
its required checks fail. These failures do not invalidate earlier macOS ARM64
results or qualify the separate IMP-008 revision.

The user requires signing before **any public CLI release**, including release
candidates. Existing ad-hoc Mach-O sealing is not a release signing identity.
The publication implementation must select and document the signing authority,
sign and verify final artifacts before promotion, and bind signatures to the
same digests carried by release manifests. Apple notarization requirements
remain a separate decision. No repository Actions secret names or protected
environments were returned by the CLI repository read-only inspection on this
date; this is not proof that organization-level signing facilities are absent.

Use npm trusted publishing through GitHub Actions OIDC, not a long-lived npm
token. The present workflow is `cli-distribution-check.yml`, a read-only
rehearsal with no npm publish step and no `id-token: write` permission. It is
**not** a valid publisher mapping. Before configuring `@openprose/prose`, add and
review the actual protected publication workflow, including its exact filename,
repository `openprose/prose-cli`, environment, signing gate, package-name
migration, immutable artifact verification, and narrowly scoped OIDC permission.
Then update the npm package's trusted publisher to that exact workflow. Do not
claim an existing mapping or ownership has been verified from a public 404.


[Verification run 35168892048](https://github.com/openprose/prose-cli/actions/runs/35168892048)
tested `2549a781498156b7677022ca63d529772c15bf4d`: macOS ARM64 passed; Linux x64
now built and reached the expected host-admission failures (70 assertions);
Linux ARM64 reached the same class of failures (101 assertions). macOS Intel
still reported `image-bundle: Bundle failed`. Therefore installing the baseline
runtime corrected Linux x64 build preparation but did not resolve macOS Intel.
The next diagnostic step is to expose the bounded Bun aggregate build error and
check the isolated compiler runtime/cache behavior. No passing check was
bypassed, and CLI PR 2 remains open and unmerged.


## Current publication decision — September 17, 2026 (UTC)

The user authorized public source and an explicitly unsigned RC under
`@openprose/prose-cli`, preserving latest-kernel startup. This supersedes earlier
name-migration and mandatory-signing-before-RC notes. See the maintained
[publication setup](cli-publication.md) for OIDC fields, actual prerequisites and
the separate Apple signing task. No CLI package has been published.
