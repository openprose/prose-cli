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
