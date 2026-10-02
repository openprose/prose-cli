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
was still running at inspection. These checks do not qualify new release bytes.

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
6. Obtain approval of that concrete candidate before creating or publishing its
   public release. Stage the exact inventory, run the protected sign-only or npm
   publication path as applicable, independently verify signatures and uploaded
   bytes, then publish the prerelease and mirror the reviewed subset. Updating
   the RC pointer requires its actual current digest as predecessor guard.
7. Verify unauthenticated public downloads, fresh installs and registry integrity
   for each channel actually published. Retain receipts permanently. Stable
   promotion is a separate decision and requires actual Apple qualification.

## Channels and owner setup

Both implementations have standalone GitHub and pkg.prose.md downloads. The
npm root `@openprose/prose-cli` launches Bun through four optional binary
packages; it does not install Rust. Root `latest` is still `0.14.0`. Public
lookups of all four platform names returned 404 on October 2; this does not
establish availability of private names or organization creation rights.

The owner already configured root OIDC. Trust belongs to each npm package,
not to an organization-wide registry repository. npm's current
[trust prerequisites](https://docs.npmjs.com/cli/v11/commands/npm-trust/)
require each package to exist first. The current split design therefore requires the
one-time owner bootstrap described in [publication setup](cli-publication.md).
A single package containing all four binaries is also possible and would use the
existing root OIDC mapping without new package bootstrap. For the published RC,
the four compressed platform tarballs total 124,136,871 bytes (about 124 MB),
versus 22–39 MB for one platform. That sum estimates combined payload size, not
a measured combined package. The current launcher and publication validator
expect five packages; changing this choice requires a new packager/launcher,
four-platform fresh-install qualification and reviewed publication policy.
No packaging change or new package name is selected by this preparation plan.
No placeholder versions. The root remains OIDC-only. After real platform
publication, configure each package with `openprose/prose-cli`,
`cli-publish.yml`, environment `publication`, direct publish permission; remove
`NPM_BOOTSTRAP_TOKEN` and revoke it. Trust administration requires owner 2FA;
the bootstrap publishing credential is not a trust-administration credential.

The owner confirmed npm setup and Apple enrollment are not ready. GitHub's
publication environment has a branch policy but no required reviewer and lists
no secrets or variables at inspection. Recheck actual environment and owner
account settings before publication; an environment name alone does not enforce
human approval. Keep credentials out of chat, Git and the model-key environment.

Apple enrollment and protected certificate/notarization setup remain IMP-015.
Use the existing unsigned-RC policy only with explicit disclosure and concrete
candidate approval. Sigstore supplies workflow/byte provenance, not Developer ID
or notarization. A signed release must use a new immutable version.

Homebrew is deferred: no accessible `openprose/homebrew-tap` was found, formula
names and default implementation remain proposals, and current formula tooling
requires stable artifacts. Adding an RC tap now would add an unqualified channel
without resolving npm or Apple prerequisites.
