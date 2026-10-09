# CLI publication setup

Status (October 2, 2026): explicitly unsigned `0.15.0-rc.1` is published on
[GitHub](https://github.com/openprose/prose-cli/releases/tag/v0.15.0-rc.1) and
[pkg.prose.md](https://pkg.prose.md/cli/releases/0.15.0-rc.1/manifest.json).
Its exact runtime source is `68297eff23990a5153e5d0a938febf68c3c4751d`.
The [publication receipt](https://github.com/openprose/openprose-distribution/blob/main/records/cli-0.15.0-rc.1.md)
records qualification and public verification. npm publication remains pending;
its root trusted publisher was configured by the owner on September 17, and
has not been independently rechecked in the owner's npm account. On October 2
the owner confirmed npm bootstrap setup and Apple enrollment are not ready.
Unsigned means no Developer ID signature or Apple notarization. Sigstore and
npm provenance do not supply Apple platform trust. See the
[next-candidate preparation](cli-release-next.md) before releasing changed source.

## Current npm layout

New publication plans use `openprose.cli-publication/2` and packages use
`openprose.npm-cohort/3`. All five tarballs have the same registry name,
`@openprose/prose-cli`. The root version (for example `0.15.0-rc.2`) depends on
exact aliases to payload versions that sort below the root, such as `0.15.0-0.rc.2-darwin-arm64`.
Users install the root version; npm selects the matching OS/CPU/libc payload.
No lifecycle script or external binary download is needed at installation.

The existing root OIDC identity below covers every version under this name.
No supporting package creation, extra trust mappings or bootstrap token is
required for this layout. The publisher forbids bootstrap for new plans,
publishes payload versions first with `platform-PLATFORM` tags, verifies their
registry integrity and publishes the root last under `rc`, `dev` or `latest`.
Payload tags never replace the root channel tags. Root trust/authentication
must still be verified through the actual protected publication workflow.

The previously reviewed `0.15.0-rc.1` plan remains schema one with five distinct
npm names. Its bytes and legacy validator are preserved. The bootstrap
instructions later in this document apply only to that historical layout;
they are not prerequisites for the new release. Stable macOS signing remains
separate. npm currently distributes Bun; Rust standalone downloads remain
independently selectable.

## Packaged SDK custody

Candidate SDK archives bind the helper, build receipt, notices and, on macOS,
the complete `prose-agents-sdk-runtime` support tree. Dedicated SDK admission
checks physical file hashes and modes, directory membership and bounded framework
aliases against the exact receipt. Generic archive admission retains its existing
member limit and refusal of links. Copying only the Mac helper is insufficient.

The Mac builder records COLLECT membership and verifies signatures and architecture
for every actual Mach-O support file before recording final bytes. Signing and
notarization consumers bind the same complete payload. Unsigned candidate checks
establish their measured ad hoc signature behavior; they do not grant Developer ID
trust, notarization or publication authority. Linux keeps its native supplier and
ELF closure evidence. See [the SDK design](packaged-sdk-design.md) for the source,
member and byte bounds and [the release guide](../cli/release/README.md) for final
qualification and publication gates.

## npm trusted publisher

Configure the existing `@openprose/prose-cli` package with this exact identity:

| Field | Value |
| --- | --- |
| Provider | GitHub Actions |
| Organization/user | `openprose` |
| Repository | `prose-cli` |
| Workflow filename | `cli-publish.yml` |
| Environment | `publication` |
| Allowed action | Direct `npm publish` |

The file is `.github/workflows/cli-publish.yml`; npm receives only its filename.
Do not authorize the distribution rehearsal workflow. Create the GitHub
`publication` environment with main-only deployment. This environment is configured; no required reviewer is currently enabled. The owner configured the root package trusted publisher on September 17, 2026. The four platform packages still require first-publication bootstrap and their own trusted publishers.
The protected, main-only publishing job uses GitHub-hosted runners, `id-token: write`, and `contents: write` because GitHub only exposes draft releases to identities with push access. Build and rehearsal jobs remain read-only. It does
not require `NPM_TOKEN`. The repository is public, as required for npm
provenance. Every generated package must declare this repository URL.

The four binary packages require separate identical trusted-publisher entries:

- `@openprose/prose-cli-darwin-arm64`
- `@openprose/prose-cli-darwin-x64`
- `@openprose/prose-cli-linux-arm64-gnu`
- `@openprose/prose-cli-linux-x64-gnu`

On the date above, the root package's `latest` was `0.14.0`; all four binary
package lookups returned 404. That does not prove ownership or availability.
Trusted publishing requires an existing package. A first-publication bootstrap
and subsequent per-package trust setup are therefore still required. Do not
publish placeholders or use a broad permanent token to bypass that prerequisite.
The root package's existing trusted publisher alone cannot create the children.

References rechecked October 2, 2026:
[npm trusted publishers](https://docs.npmjs.com/trusted-publishers/),
[npm trust prerequisites](https://docs.npmjs.com/cli/v11/commands/npm-trust/),
[npm provenance](https://docs.npmjs.com/generating-provenance-statements/).
Recheck these before changing workflow identity or credential policy.

## Publication boundary

The manual workflow consumes a reviewed plan from `cli/release/plans/` on main
and immutable reviewed release artifacts. It does not build, execute or rewrite downloaded
binaries. The validator binds the version, source, protected preflight, kernel
qualification and every artifact hash. It checks the complete platform inventory
before download and again locally. npm and standalone Bun bytes must agree.
Packages are published platform-first and root-last; an existing version is
accepted only if its integrity matches the exact reviewed tarball. Failed
registry requests are not treated as proof that a version is absent.

Only an explicit `X.Y.Z-rc.N` plan may select `signing: unsigned-rc`. That is the
owner-approved exception. Stable publication still requires the Apple signing
path. All artifacts receive detached Sigstore signatures in the publishing job;
those and the npm result are retained as workflow evidence. They must also be
retained with the public release before claiming complete download verification.
Do not promote an incomplete release or replace npm's `latest` tag with an RC.

The existing fixed-image full-release preflight is not proof of moving-kernel
startup. The separate `kernel-rc` build/package path verifies release profile,
disabled test seams, latest-kernel policy and fresh offline installations on all
four platforms. `cli-kernel-rc.yml` runs on PRs for validation and manually from
main for actual candidates. `assemble_kernel_rc.py` verifies the native reports
and package bytes; without exact-binary live smoke evidence, it emits an
unqualified development plan that publication refuses. Do not satisfy the gate by embedding a fixed kernel, relabeling
an echo/sentinel fixture, or fabricating a protected passing report. The reviewed [0.15.0-rc.1 plan](../cli/release/plans/0.15.0-rc.1.json) binds the qualified main-build artifacts and paired live smoke evidence. The old functional-alpha promotion workflow is retired; this is the maintained release path. Source admission and publication remain separate.

## Deferred macOS signing

`cli/ci/sign_macos.py` signs copied Bun/Rust binaries in a fresh directory,
checks Developer ID, team, hardened runtime, timestamp and entitlements, and
submits a ZIP for notarization. It accepts a receipt only when Apple's response
binds the exact archive hash. `--verify-existing` rechecks the same evidence
without signing or submitting again. Its tests use fake native commands; live
Apple qualification has not occurred.

The caller creates, unlocks and removes a temporary signing keychain. Required
future environment secrets are `APPLE_DEVELOPER_ID_P12_BASE64`,
`APPLE_DEVELOPER_ID_P12_PASSWORD`, `APPLE_NOTARY_KEY_P8`,
`APPLE_NOTARY_KEY_ID`, and `APPLE_NOTARY_ISSUER_ID`; non-secret variables are
`APPLE_TEAM_ID` and `APPLE_SIGNING_IDENTITY`. Do not store these in the model
API-key `.env`. Sign before packaging and publish a new version after signing;
never replace an unsigned version. See the workspace
[Apple signing task](https://github.com/openprose/openprose-workspace/blob/main/work/items/IMP-015.md).

## Local validation

Run the publication and signing unit tests without credentials:

```sh
python3 -m unittest discover -s cli/ci -p test_publication.py
python3 -m unittest discover -s cli/ci -p test_sign_macos.py
```

The fixtures are synthetic and must never become publication plans. Native
installation qualification, a real reviewed plan, registry bootstrap/OIDC,
public byte verification and receipt retention are separate acceptance steps.


## One-time platform package bootstrap

The normal `cli-publish.yml` path uses npm trusted publishing (OIDC). Trust is
configured separately for each package. npm requires a package to exist before
trust configuration or staged publishing; trust on `@openprose/prose-cli` does
not authorize creation of the four platform package names.

For the first qualified release only, an owner may explicitly dispatch
`bootstrap_platform_packages=true` and supply the temporary environment secret
`NPM_BOOTSTRAP_TOKEN`. It must be a short-lived npm granular credential with
permission to create public packages in the OpenProse scope and to publish
without interactive 2FA in this job. This credential is not a model API key.
Do not place it in the workspace model `.env` or in repository files.

The publisher first checks the exact versions and package names on the official
registry. A name-level E404 is required for the bootstrap route; other registry
errors stop publication. An existing platform package uses OIDC even when the
bootstrap option is selected. The root package always uses OIDC and cannot be
bootstrapped by this path. Each absent name is checked again immediately before
publication; a newly existing name stops the bootstrap attempt without fallback.
A public lookup cannot establish ownership of an inaccessible private name, so
the owner must confirm that these four intended names are new and authorized.

The token is removed from the process environment before other commands run.
Only the selected first-publication subprocess receives a private temporary npm
configuration file (mode 0600 inside a mode 0700 directory). Normal OIDC
subprocesses receive an empty, isolated npm configuration. Configuration,
cache, and authentication files are removed on success or failure. An OIDC
authentication failure is never retried with the bootstrap token. All routes
publish the same reviewed tarballs with mandatory provenance from GitHub
Actions; this path creates no placeholder version and does not disable artifact
verification, signing policy, or exact-binary live evidence requirements.

After a successful bootstrap, configure each platform package's trusted
publisher with repository `openprose/prose-cli`, workflow `cli-publish.yml`,
environment `publication`, and publish permission. Remove the temporary secret
and revoke its npm credential. Future releases leave the bootstrap input false.
A partial run can resume: already published exact bytes are verified and skipped;
existing names with unpublished versions use OIDC. The receipt records the
credential route for each package without recording credentials.

References rechecked 2026-10-02: [npm trust](https://docs.npmjs.com/cli/v11/commands/npm-trust/)
and [staged publishing](https://docs.npmjs.com/staged-publishing/).

## Signing direct downloads while npm is unavailable

The manual workflow defaults to `operation=publish`. An explicitly selected
`operation=sign-only` applies the same main-workflow identity, public repository,
qualified artifact inventory, live evidence, and macOS policy gates. It signs
and verifies every reviewed artifact with Sigstore, without registry lookups or
npm publication. Bootstrap authorization and credentials are forbidden in this
mode. Its receipt records `operation: sign-only`, `npmStatus: not-published`, and
`githubReleasePromoted: false`; it must not be presented as npm success.

After verifying the workflow result, download its receipt and signature bundles,
verify each bundle against its exact release asset and publisher identity, and
attach the signature bundles to the draft GitHub release; retain the receipt as workflow and workspace evidence. Only then promote the qualified draft
and mirror a reviewed subset through distribution. A later normal publication
run may fetch a draft or an already public unsigned RC marked as a prerelease.
The tag and source must match the reviewed plan, and the entire original
inventory must retain its exact sizes and digests. Only recognized detached
`<original asset>.sigstore.json` bundles, each at most 1 MiB, may be additional
release assets. They are not downloaded or trusted by this fetch: the publisher
verifies and signs the original bytes again. Published stable releases and
unknown additional assets remain forbidden. npm is a separate incomplete channel
until its actual publication and public integrity checks succeed.

### Empty evidence files

GitHub release uploads reject zero-byte files. The reviewed inventory retains empty build logs with size zero and the SHA-256 of the empty byte string. Fetching reconstructs only those explicitly declared evidence files; it never reconstructs missing nonempty files or executable packages. Native build reports still bind their exact original bytes. Detached signatures cover the reconstructed empty files too.

## Resume an interrupted draft asset upload

Use `python3 cli/ci/stage_upload.py --plan PLAN --artifacts DIRECTORY` after the
qualified draft exists, whether empty or partially uploaded. The
helper repeats local qualification, verifies the tag's source commit, and checks
the complete paginated GitHub asset inventory. It skips an existing asset only
when its uploaded state, byte size, and GitHub SHA-256 digest all match the plan.
Unknown assets, missing digests, mismatches, and incomplete `starter` assets stop
for operator inspection. It never deletes or overwrites an asset.

Declared zero-byte evidence is verified locally against the empty SHA-256 and
retained virtually; GitHub does not accept empty asset uploads. Empty packages
are forbidden. The final remote inventory contains only nonempty plan entries.
Uploads are sequential. Only HTTP 429, 500, 502, 503, and 504 receive bounded
retries: at most three attempts with 2-second and 8-second delays. Every response
is reconciled against GitHub because an error may arrive after a successful
upload. Authentication errors, permanent failures, and unconfirmed timeouts stop.
The helper verifies the complete final inventory and leaves the release a draft;
it does not publish npm packages, promote GitHub releases, or change channels.
