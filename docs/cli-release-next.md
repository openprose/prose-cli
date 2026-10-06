# CLI release status and next candidate — October 5, 2026

Published RC2 is behind current main runtime after [PR33](https://github.com/openprose/prose-cli/pull/33) merged on October 5. See the [current source/release difference](#current-source-and-release-difference) below.

Unsigned `0.15.0-rc.2` is published on
[npm](https://www.npmjs.com/package/@openprose/prose-cli/v/0.15.0-rc.2) and as a
[GitHub prerelease](https://github.com/openprose/prose-cli/releases/tag/v0.15.0-rc.2).
The reviewed [publication inventory](../cli/release/plans/0.15.0-rc.2.json) binds
all original bytes. Public verification passed for all five npm versions and
191 GitHub assets, all npm provenance signatures and 103 artifact signatures.
A fresh npm install with ignored lifecycle scripts passed signature audit and
offline release probes. The guarded [mirror promotion](https://github.com/openprose/openprose-distribution/actions/runs/37073561418)
succeeded; all 32 mirrored files, its manifest/RC pointer and both fresh macOS
ARM64 standalone binaries passed independent public/offline checks. Permanent
[publication custody](https://github.com/openprose/openprose-expedition/tree/6bea65bc6908db299e74dbbcd3e912309d3e8f6b/imp-014-npm-release-oct02/publication/0.15.0-rc.2)
retains receipts, original signature/provenance bundles, stopped checkpoints,
public verification reports and their programs.

```sh
npm install -g @openprose/prose-cli@0.15.0-rc.2 --ignore-scripts
```

`rc` selects the ordinary root version; `latest` remains `0.14.0`. Preserve
immutable `0.15.0-rc.1` and `0.15.0-rc.2` bytes. Subsequent candidates require an
unused version and their own exact-source qualification; neither the consumed
RC2 label nor its completed two-attempt live allocation is reusable.

## Release selection

Runtime source is frozen at `fe8b50328d87a9f60f3bbf2d527edffa56f3bd12` after
[PR32](https://github.com/openprose/prose-cli/pull/32) passed all fifteen checks.
The integrated tree equals that tested candidate. The explicit
[four-platform release build](https://github.com/openprose/prose-cli/actions/runs/37065138182)
passed and produced the original thirteen install archives. Independent npm
identity and Bun-byte equality checks passed. The
[paired live smoke](https://github.com/openprose/openprose-expedition/blob/ae247835b66f9d420158dbf71cb021acca357a33/imp-014-npm-release-oct02/evidence/0.15.0-rc.2/live-smoke.json)
passed on those exact macOS ARM64 binaries.
[Exact-main source admission](https://github.com/openprose/prose-cli/actions/runs/37065118159)
and complete inventory review passed before publication.
[Protected publisher run 37072191629](https://github.com/openprose/prose-cli/actions/runs/37072191629)
succeeded from publisher-only main `0916aee46efc530b56748eead3a2252de59d8b39`.
Earlier exact macOS payloads retain their original main-workflow provenance;
recovery reused their matching bytes. Keep runtime and publisher identities
separate in receipts.

The finance/context research owner is
investigating Prime event-history compatibility under IMP-083. Its longer probe
found missing tool-start events, and the compatibility candidate remains
unqualified. Do not merge it, weaken parsing or count its paid research runs as
release qualification. Refresh its workspace record and current remote main
before selecting source. Either integrate an independently qualified repair or
explicitly disclose the affected Prime route and review release scope.
IMP-082's reproduced OMP cleanup defect is repaired and qualified on integrated
main. Its distinct historical Bun timing observation remains unexplained; the
release does not claim that separate observation repaired.

Keep the published kernel default `0.1.0-rc.1`. The failed semantic qualification
of kernel `0.2.0-rc.1` is separately owned and cannot be bypassed by this release.
A CLI version pins executable bytes; normal startup can still select a moving
kernel. Record the observed kernel identity in the new live evidence.

## Gates for the next unused candidate

1. Review the final changes and exact remote-main source, including the CLI
   changelog and known limitations. Confirm the unused candidate label and all
   source-admission, native rehearsal and CodeQL results for that exact commit.
2. Set `cli_candidate_version` to a verified unused candidate label, then
   dispatch its nonpublishing build from main:

   ```sh
   gh workflow run cli-kernel-rc.yml --repo openprose/prose-cli --ref main \
     --field version="$cli_candidate_version"
   ```

   Read the run's actual source SHA immediately. If main moved, inspect the new
   source and gates; do not treat the previously inspected head as its identity.
   Download all four platform artifacts from that one run and verify native
   reports, standalone and npm inventories. Never rebuild between qualification
   and publication or reuse automatic runs labeled with the old public version.
3. Assemble an unqualified development inventory first. Freshly install both
   standalone implementations and the local Bun npm cohort. Native reports must
   bind the exact packaged executable hashes, release profile and disabled test
   seams. No model call is authorized by this step.
4. Record a separate, explicit live-test allocation before any model calls.
   Specify actual admitted harness/model, attempt and timeout limits, spending
   ceiling and billing route. Finance's USD 200 research allocation does not
   apply. Retain exact-binary paired smoke evidence in an isolated lab branch,
   including failed attempts, source, package and observed kernel identities.
5. Use `assemble_kernel_rc.py` with that immutable evidence to create the final
   publication JSON. Review the complete artifact inventory, npm/standalone Bun
   equality, signing policy and release notes through a CLI PR. Export and review
   the curated distribution plan separately. No qualification sentinel or mock
   report may stand in for actual release evidence.
6. The October 2 assignment completed RC2. For the next release, establish its
   publication assignment and review the concrete inventory against that scope
   before creating or publishing its public release. Stage the exact inventory, run the protected sign-only or npm
   publication path as applicable, independently verify signatures and uploaded
   bytes, then publish the prerelease and mirror the reviewed subset. Updating
   the RC pointer requires its actual current digest as predecessor guard.
7. Verify unauthenticated public downloads, fresh installs and registry integrity
   for each channel actually published. Retain receipts permanently. Stable
   promotion is a separate decision and requires actual Apple qualification.

## npm packaging direction after owner-time clarification

The user places a high premium on owner time and accepts additional agent
engineering work to remove manual prerequisites. RC2 implements a Codex-style
npm layout: one registry identity, `@openprose/prose-cli`, with
platform payloads at exact platform-suffixed versions and dependency aliases.
Users still install the ordinary root version and receive one platform binary.
The complete cohort is qualified and published using the existing root OIDC
trust. No new npm package names or bootstrap token were needed.

Observed October 2 peer metadata: Claude Code `2.1.287` and OpenCode `1.18.34`
use separately named optional platform dependencies. Codex `0.160.0` instead
aliases platform dependencies to versions under `@openai/codex`; for example
`@openai/codex-darwin-arm64` resolves to
`npm:@openai/codex@0.160.0-darwin-arm64`. The platform manifest retains its actual
name `@openai/codex` and OS/CPU selectors. Prime Agent's current main recommends
its own installer and served platform archives, rather than npm.

Retain existing immutable releases and their split-package validation. The
implemented layout uses explicit cohort/schema discrimination rather than
interpreting old plans differently. Keep the packager, launcher, publisher,
report bindings and installation tests aligned in future changes. Validate actual registry name and suffixed version,
source/cohort identity, OS/CPU/libc selectors and exact binary bytes, while
resolving the installed dependency alias directory safely. Preserve no-lifecycle-
script installation and npm/standalone Bun equality.

Prerelease payload versions begin with a numeric `0` prerelease component
(for example `0.15.0-0.rc.2-darwin-arm64`), so npm ranges select the ordinary
root instead of a payload-only version. Stable payload versions retain the
platform suffix below the stable root. Verify this with npm's own resolver.

Publish all platform payload versions under a dedicated platform tag, then the
root version last under its intended RC/stable tag. npm may accept a PUT with
HTTP 202 before public metadata is visible: bounded read-only polling must
verify every payload before submitting the root. Never replay a publish during
visibility polling or infer an integrity conflict merely from temporary absence. Platform publishes must never
move `latest` or `rc` to a payload-only version. Preflight every exact version,
refuse mismatched existing bytes, support partial-resume integrity verification
and retain receipts per package name/version. Require the existing root name to
exist and use only OIDC; no token fallback. Fresh native installs on all four
platforms must pass before a new reviewed release plan is proposed. Root OIDC authentication is confirmed by the actual protected RC2 runs and
public provenance. Future releases must still use the exact configured main
workflow/environment identity.

## Channels and owner setup

Both implementations have standalone GitHub and pkg.prose.md downloads. npm
currently packages the Bun implementation; selecting Rust through npm is a
separate product decision. Public root `latest` remains `0.14.0`, and all four
original supporting package lookups returned 404 on October 2. That is a
historical split-design bootstrap prerequisite, not a blocker for the selected
same-name layout. The reviewed new schema and publisher reuse the existing root trust;
[publication setup](cli-publication.md) also preserves the historical route.

The publication environment lists no secrets or variables and has a branch
policy without a required reviewer. Root OIDC was configured by the owner and used successfully. The same-name
layout reuses that identity without new-package setup. Check workflow identity
and exact version availability for future releases.
Keep credentials out of chat, Git and the model-key environment.

Apple enrollment and protected certificate/notarization setup remain IMP-015.
Use the existing unsigned-RC policy only with explicit disclosure and concrete
candidate approval. Sigstore supplies workflow/byte provenance, not Developer ID
or notarization. A signed release must use a new immutable version.

Homebrew preparation is now user-assigned. The selected formula names are
`prose-bun` and `prose-rust`, both providing `prose`; no implementation default
or plain `prose` alias is chosen. Prerelease status stays in the version and
caveats. [Installation and switching](cli-distribution.md#homebrew-installation-and-implementation-selection)
uses native Homebrew unlink/link and preserves immutable RC2 bytes. Apple
signing remains separate; Homebrew does not remove unsigned-RC limitations.

## October 5 runtime parity audit

[The provider-free parity audit](cli-rc2-parity-oct05.md) confirms published RC2
and main `39c90f45` share runtime source `fe8b5032`. Fresh public Bun/Rust ARM64
binaries and npm archive match the original inventory. The audit accounts for
all local/remote CLI heads and 30 clean registered worktrees. PR33 remains an
unqualified, owner-reviewed runtime candidate; historical documentation and
diagnostic branches contain no missing validated runtime. Pin explicit RC2
version, executable hash and separate kernel identity for the finance demo.
No paid calls or immutable release changes were made.

## Current source and release difference

[PR33](https://github.com/openprose/prose-cli/pull/33) merged as
`a20ee77529b557a13b8d30bea0cae6c505d4efb8` after all fifteen checks passed on
head `687523c2d693b8cefa24bfe80b0d165db68be6ee`; the integrated tree equals that
checked head. [Admission custody](https://github.com/openprose/openprose-workspace/blob/64114d2/work/plans/IMP-083/evidence/README.md)
retains both 53-gate source logs and the integration receipt. Main now includes
bounded recovery for completed, declared Prime tools whose omitted result
notifications are corroborated by final history. RC2 runtime remains
`fe8b50328d87a9f60f3bbf2d527edffa56f3bd12` and does not contain this fix.

The [earlier parity audit](cli-rc2-parity-oct05.md) applies to its main snapshot
`39c90f45` and documentation integration `13d3894`. It is historical evidence,
not a claim of parity after PR33. Broader missing native declarations/starts and
full finance/document-workflow qualification remain separate. A demo requiring
PR33 must identify an explicitly selected main development build; it cannot
claim to use the unchanged public RC2 executable.

Kernel qualification also retains an unresolved `PROCESS_CLEANUP_FAILED`
observation from a private fixed-image Bun build based on released source. That
private executable differs from published RC2 bytes. Native exit zero and
terminal completion did not establish successful cleanup.
[Retained diagnosis and controls](https://github.com/openprose/openprose-expedition/blob/3f37312fe9c5f8fefc5176f5e1b010d1f58ef154/kernel-evaluation-outcome-v1/qualification-oct05/review/cleanup.md)
distinguish controlled transient-group sensitivity from reproduction of the
original failure; its cause remains unresolved. PR33 does not repair or waive
that failure, and kernel qualification remains stopped.

No successor release, new paid qualification or consumer upgrade was authorized
by this integration. A subsequent publication requires an unused version and
its own exact-source/artifact qualification and allocation. Preserve RC2
labels, bytes and the closed two-attempt live allocation.
