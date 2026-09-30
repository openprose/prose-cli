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
