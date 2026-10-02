# CLI release path

The maintained release path builds one current CLI candidate, qualifies its exact
bytes, and publishes only through the protected manual workflow. The retired
functional-alpha promotion and post-publication workflows are not supported.
There are no current CLI customers requiring those flows. Historical results
remain available in Git and do not qualify a new source revision.

## Source admission

From a clean checkout with the pinned tools and hash-locked dependencies in
[the contributor guide](../CONTRIBUTING.md), run:

```sh
python3 cli/ci/run_local.py
```

[CLI source admission](../../.github/workflows/cli-ci.yml) runs the same
provider-free command on Linux x64 and macOS ARM64, on pull requests and main
pushes. [Distribution rehearsal](../../.github/workflows/cli-distribution-check.yml)
checks fresh Rust, Bun and npm installations across all four supported native
platforms. These are mechanical checks; they do not make model calls or establish
language semantics, live reliability or publication approval.

The shared installed-process corpus is exercised on all three distribution
surfaces. Corpus changes require corresponding current rehearsal counts; old
reports remain tied to the corpus and revision they actually measured.

## Development packaging

For a nonpublishing local package rehearsal, use the standalone build driver:

```sh
python3 cli/ci/build_local.py --candidate both --package /tmp/openprose-cli-artifacts
```

Its ordinary explicit-image development package embeds `echo-v0`, with test
seams disabled. A sentinel/test-seam build is only a transport diagnostic and
cannot supply package bytes. Neither fixed-image development mode qualifies
published-kernel startup; the kernel candidate path below owns that check.

## Build and qualify exact release bytes

[The kernel candidate workflow](../../.github/workflows/cli-kernel-rc.yml)
builds unsigned candidates on Linux x64/ARM64 and macOS x64/ARM64. It verifies
release profiles, disabled test seams, current published-kernel startup, and
fresh standalone and npm installations. Its PR and main-push runs are checks.
A manual main run takes an exact `X.Y.Z-rc.N` candidate version.

`cli/ci/build_kernel_rc.py` creates the native packages and build evidence.
`cli/ci/assemble_kernel_rc.py` checks the complete four-platform inventory,
source identity and artifact hashes before producing a plan. Exact-binary live
smoke evidence is a separate gate: an unqualified development plan is refused
by publication. Do not reuse an earlier plan to qualify changed source or
replace real qualification with echo/sentinel fixture evidence.

`cli/ci/package_local.py` creates deterministic standalone archives, npm
meta/platform packages, checksums, dependency inventories, SBOMs, provenance and
release manifests from already-built executables. Repackaging the same inputs
does not establish compiler reproducibility. The source admission and rehearsal
checks retain that distinction.

The sentinel image is test-only and cannot enter public artifacts. Ordinary
published clients use production service endpoints; custom endpoints belong in
explicit developer builds, which cannot be marked release eligible.

## Publication and prerequisites

[The publication workflow](../../.github/workflows/cli-publish.yml) is manual,
main-only, serialized and attached to the `publication` environment. It consumes
a reviewed JSON plan from [plans/](plans/README.md), fetches the exact draft
artifacts, verifies their identity and hashes without executing them, checks
platform trust, signs artifact digests, and publishes the reviewed npm bytes.
It does not rebuild downloaded artifacts. Platform packages publish first and
the root package last. Existing versions must have exactly matching integrity.

New candidates use one npm identity, `@openprose/prose-cli`, for the launcher
and four platform payload versions. Exact optional dependency aliases select
one executable. Prerelease payload versions sort below the launcher, so npm
ranges cannot select a payload-only version. Platform tags remain separate from
`rc` and `latest`, and the root publishes last through the existing OIDC trust.
No bootstrap token or new package-name setup is needed for this layout.

Historical schema-1 plans retain their original separate platform identities
and first-publication controls. See [publication setup](../../docs/cli-publication.md)
for both formats and the exact workflow/environment identity. Missing trust is
a publication blocker, not a reason to weaken CI or add permanent tokens.

No publication is authorized merely by passing CI or merging a PR. The intended
next release requires a fresh reviewed plan and a separate explicit release
decision. Stable macOS artifacts require Developer ID signing and notarization;
the existing unsigned-RC exception does not qualify a stable release. npm
provenance and Sigstore signatures do not remove that macOS limitation.

## Evidence and history

Retain exact source/artifact identities, all failed attempts, platform results
and qualification limits in the owning repository. CI diagnostics are uploaded
even on failure. Machine-specific paths, credentials and raw provider output
must not enter public records.

[The retired release documentation](https://github.com/openprose/prose-cli/tree/6d7e8eb2ced840d443489593d29ca5f058090bd7/cli/release)
is historical context only. Maintained release-facing changes belong in the
[CLI changelog](../CHANGELOG.md), and new qualification belongs with the exact
new candidate rather than rewriting earlier evidence.
