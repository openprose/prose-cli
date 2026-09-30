# IMP-061 current CI qualification

Raymond's hosted-service and Claude compatibility changes, together with the
provider-free corrections recorded in the sibling qualification documents,
are on main at `6d7e8eb2ced840d443489593d29ca5f058090bd7` through
[PR 12](https://github.com/openprose/prose-cli/pull/12). PRs 10 and 11 are merged.
Fresh main CodeQL run 36777773864 passed; no alert was manually dismissed.

This follow-up replaces the missing, retired alpha workflows with structural
admission of the four maintained workflows. Full source admission runs on
Linux x64 and macOS ARM64; distribution rehearsal and unsigned kernel candidate
construction cover all four supported native targets. Public publication remains
manual, restricted to main and the publication environment. No package is
published by source or packaging CI.

The source repair adds the generic SDK's missing provider-free oracle scenario,
fixes its invalid isolation labels, and retains its distinct unmeasured status.
It removes Undici's builder filename from compiled Bun diagnostics without
changing the explicit transport. Installed rehearsal expects the current 50
shared cases. Strict Rust formatting and Clippy remain required.

Unused alpha promotion, post-publication alpha verification, frozen alpha registry
lineage and historical evidence enrichment are retired. They remain available in
Git history at the integration revision above. Current publication and package
custody contracts stay in admission. Missing historical live records are replaced
in ordinary tests by explicitly synthetic matrix and capture records; these do
not claim new live qualification or restore legacy evidence formats.

## Validation in progress

Current workflow policy: 13 tests pass. Adapter oracle: 16 tests pass. Release
rehearsal contract: 16 tests pass. Compiled Bun standalone: 6 tests pass. Strict
Rust Clippy passes with test seams. The full provider-free admission run and
fresh remote matrices are still pending; this record will be updated with the
actual reviewed revisions and outcomes before integration.

No paid provider invocation, package publication or deployment is assigned.
Release prerequisites, including npm package ownership/trusted publishing and
platform signing, remain separate gates for the future release.

## Subsequent findings

Run 36782446830 passed unsigned kernel candidate qualification on all four
platforms. Distribution run 36782446725 passed on macOS ARM64/x64 and Linux
ARM64, but its Linux x64 developer tests exposed Darwin-only assumptions. Source
run 36782446728 also caught a diagnostic string accidentally reformatted by a
lint repair. Both failures are retained; neither is counted as qualification.

Installed tests now distinguish recipe-supported hosts from unsupported-host
refusal. Unit process fixtures explicitly inject the admitted Darwin ARM64
identity, while compiled adversaries use their actual host. The four-harness
adversary builds isolated echo fixtures rather than substituting ordinary
published-kernel candidates or making a network/provider call. SDK oracle tests
remain separate from that installed cohort.

The adversary exposed OMP envelope-mode tool admission and Codex launch parity
mismatches. Both products now follow the shared recipe and native-mode contract.
Process cleanup uses a distinct typed process resource so it cannot collide with
the hosted service's strict not-found resource object. Current packaging tests
retain generated archive/npm custody, remove tests of deleted historical guide
examples, and expect the new service account readiness and Undici inventory.

Two open CodeQL findings have source repairs: test newline mutation replaces all
newlines; the experimental checkpoint lexer advances linearly through escaped
strings after JSON grammar validation. A fresh scan must verify closure; no
finding has been manually dismissed. All new qualification is still pending.

The remaining unused numbered-alpha package admission and draft assembly helpers
are retired rather than repaired to preserve their frozen alpha journey. No
maintained workflow invokes them. Current kernel assembly, publication verifier,
release-package admission, generated package guidance/custody and installed
rehearsal retain the relevant release protections.

Fresh source run 36786514562 exposed the Windows helper toolchain target being
requested after admission disabled the network. Source preparation now preloads
that declared target; a workflow negative test rejects its omission. The exact
release-package help identity is refrozen to Raymond's current service commands
(7,486 bytes, SHA-256
`d58587215fa0a5182433a81763c3a70370a92f7926c83f0b4187069025656096`).
The package account-status assertion uses the current service envelope and zero
exit for logged-out status; the default unavailable-harness doctor still exits
10. Two fresh installed-package machine-surface checks pass.

The last local benchmark gate depended on another absent historical migration
record. Those historical assertions are retired; current fixture collection now
checks manifest digests, byte-identical reanalysis and privacy. That check caught
absolute image/program/validator paths in exported identities. Admission still
verifies exact input bytes, but public descriptors now use portable paths. All
34 current benchmark contract tests pass. The fixture process is explicitly not
a CLI performance measurement.

Linux source run 36787737698 reached Rust tests and exposed three Linux Secret
Service assertions still using the former account envelope and unstructured
malformed-key reason. Those tests now follow the current service envelope and
structured credential fields, retaining secret-tool call, shadow resistance,
missing-D-Bus and recovery checks. This is a test update, not a legacy fallback.
Fresh Linux execution remains required.

The clean local run begun at runtime-source revision
`9c83400402a0e229b8415910fef249c1bff54ebd` passed all 53 provider-free
gates. It includes 717 Bun tests (714 pass, 3 recorded/platform skips), both
normal/developer Rust suites and strict lint, 17 compiled adapter adversaries,
1,089 hosted cases and 21 registry cases per product, exact-package custody and
the three-surface 50-case rehearsal. Subsequent edits are Linux-only credential
assertions and adapter host selection; runtime sources are unchanged. Focused
format and both strict Clippy builds pass after the Linux test update.

Fresh CodeQL run 36787732733 completed all four analyses and PR findings are
empty. Native Linux source admission and the final head matrices remain pending.
OMP's Linux environment-key isolation and early-EOF fixture checks remain active
even though Prime-specific native cases are not admitted on Linux.

Native Linux source run 36789532647 passed the credential tests and then found
two remaining core tests expecting config validation before host admission.
Both now assert the shared unsupported-host error and zero discovery on Linux;
admitted hosts retain the original model/auth validation assertions. All other
299 Linux core tests passed in that attempt; the repaired suite needs fresh
Linux qualification. No production validation order is changed.
