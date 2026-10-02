# Next CLI release preparation — October 2, 2026

This is a preparation plan, not a qualified publication inventory or release
approval. Preserve public `0.15.0-rc.1`; changed source requires a new version.
The proposed next label is `0.15.0-rc.2`, subject to final compatibility review.
Do not dispatch the publisher using this document.

## Release selection

The inspected main is `cc5788abfc45dfe67d47e277264d87cdb0614e4c`.
Its [four-platform build](https://github.com/openprose/prose-cli/actions/runs/37052057798),
[installation rehearsal](https://github.com/openprose/prose-cli/actions/runs/37052057797)
and CodeQL pass. [Source admission](https://github.com/openprose/prose-cli/actions/runs/37052057833)
also passed. These checks do not qualify new release bytes.

Do not freeze this revision prematurely. The finance/context research owner is
investigating Prime event-history compatibility under IMP-083. Its longer probe
found missing tool-start events, and the compatibility candidate remains
unqualified. Do not merge it, weaken parsing or count its paid research runs as
release qualification. Refresh its workspace record and current remote main
before selecting source. Either integrate an independently qualified repair or
explicitly disclose the affected Prime route and review release scope.
The separately recorded OMP cleanup reliability issue also remains a limitation;
a later passing cohort does not establish its cause repaired.

Keep the published kernel default `0.1.0-rc.1`. The failed semantic qualification
of kernel `0.2.0-rc.1` is separately owned and cannot be bypassed by this release.
A CLI version pins executable bytes; normal startup can still select a moving
kernel. Record the observed kernel identity in the new live evidence.

## Candidate gates and sequence

1. Review the final changes and exact remote-main source, including the CLI
   changelog and known limitations. Confirm the unused candidate label and all
   source-admission, native rehearsal and CodeQL results for that exact commit.
2. Dispatch the nonpublishing build from main with an explicit candidate label:

   ```sh
   gh workflow run cli-kernel-rc.yml --repo openprose/prose-cli --ref main \
     --field version=0.15.0-rc.2
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
6. The owner authorized completing the latest qualified npm release on October 2.
   Review the concrete inventory against that scope before creating or publishing
   its public release. Stage the exact inventory, run the protected sign-only or npm
   publication path as applicable, independently verify signatures and uploaded
   bytes, then publish the prerelease and mirror the reviewed subset. Updating
   the RC pointer requires its actual current digest as predecessor guard.
7. Verify unauthenticated public downloads, fresh installs and registry integrity
   for each channel actually published. Retain receipts permanently. Stable
   promotion is a separate decision and requires actual Apple qualification.

## npm packaging direction after owner-time clarification

The user places a high premium on owner time and accepts additional agent
engineering work to remove manual prerequisites. Pursue a Codex-style npm layout
for the next candidate: one registry identity, `@openprose/prose-cli`, with
platform payloads at exact platform-suffixed versions and dependency aliases.
Users still install the ordinary root version and receive one platform binary.
This is an implementation direction, not completed qualification or approval
to publish. Do not request a bootstrap token for the new layout.

Observed October 2 peer metadata: Claude Code `2.1.287` and OpenCode `1.18.34`
use separately named optional platform dependencies. Codex `0.160.0` instead
aliases platform dependencies to versions under `@openai/codex`; for example
`@openai/codex-darwin-arm64` resolves to
`npm:@openai/codex@0.160.0-darwin-arm64`. The platform manifest retains its actual
name `@openai/codex` and OS/CPU selectors. Prime Agent's current main recommends
its own installer and served platform archives, rather than npm.

Retain existing immutable releases and their split-package validation. The new
layout needs explicit cohort/schema discrimination rather than interpreting old
plans differently. Extend the packager, launcher, publisher, report bindings and
installation tests together. Validate actual registry name and suffixed version,
source/cohort identity, OS/CPU/libc selectors and exact binary bytes, while
resolving the installed dependency alias directory safely. Preserve no-lifecycle-
script installation and npm/standalone Bun equality.

Publish all platform payload versions under a dedicated platform tag, then the
root version last under its intended RC/stable tag. Platform publishes must never
move `latest` or `rc` to a payload-only version. Preflight every exact version,
refuse mismatched existing bytes, support partial-resume integrity verification
and retain receipts per package name/version. Require the existing root name to
exist and use only OIDC; no token fallback. Fresh native installs on all four
platforms must pass before a new reviewed release plan is proposed. Existing
root trust is owner-recorded; actual publisher authentication remains a live
release gate, not guaranteed by metadata inspection.

## Channels and owner setup

Both implementations have standalone GitHub and pkg.prose.md downloads. npm
currently packages the Bun implementation; selecting Rust through npm is a
separate product decision. Public root `latest` remains `0.14.0`, and all four
original supporting package lookups returned 404 on October 2. That is a
historical split-design bootstrap prerequisite, not a blocker for the selected
same-name layout. Preserve [publication setup](cli-publication.md) for the
existing plan until the new schema and publisher are reviewed.

The publication environment lists no secrets or variables and has a branch
policy without a required reviewer. Root OIDC was configured by the owner;
recheck actual trust and workflow identity before release. The new layout should
reuse that one identity and remove new-package setup from the owner's path.
Keep credentials out of chat, Git and the model-key environment.

Apple enrollment and protected certificate/notarization setup remain IMP-015.
Use the existing unsigned-RC policy only with explicit disclosure and concrete
candidate approval. Sigstore supplies workflow/byte provenance, not Developer ID
or notarization. A signed release must use a new immutable version.

Homebrew is deferred: no accessible `openprose/homebrew-tap` was found, formula
names and default implementation remain proposals, and current formula tooling
requires stable artifacts. Adding an RC tap now would add an unqualified channel
without resolving Apple prerequisites.
