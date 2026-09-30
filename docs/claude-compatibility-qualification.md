# Claude compatibility corrective qualification — September 30, 2026

This corrective branch starts from Claude adapter PR 11 at `0ad4cfc`. The
recipe schema now permits an optional canonical stable `minimumVersion`.
Schema tests reject malformed, incomplete, numeric, and prerelease floors.

Two shared installed-process cases exercise Claude 2.1.282 with an unknown
event type and an unknown system subtype. Both products run the same fake
harness and input; neither product supplies the other's expected output.
The host oracle keeps these cases blocked on unadmitted platforms. Product
unit tests continue to cover missing/wrong sessions and terminal boundaries.
Contributor guidance now separates compatibility permission from exact
measured live qualification. Historical live-alpha records remain unchanged.

## Verification and limits

Passed locally on macOS ARM64: 29 shared-contract/schema tests, 46 corpus-runner
tests, both new cases against compiled Rust and Bun products with clean
differential output, Bun type checking, and 144 focused Bun adapter tests
(one recorded-stream test skipped). The original PR's Rust runner/supervisor
tests also passed locally, including 187 runner-core tests.

The adapter-oracle gate still has one failure and three errors for stale
Agents SDK inventory assumptions. The same failures were reproduced on
unmodified main `6394f88`; this correction does not hide or repair that
separate baseline debt.

The full local admission command also stops at architecture-tests with 45
missing-legacy-workflow errors, reproduced unchanged on main `6394f88`. Those
workflows were intentionally not migrated. Full local admission is not green;
this correction does not restore obsolete workflows to satisfy their tests.

Exploratory shared cases for missing/wrong sessions confirmed rejection in both
products but exposed differing process/terminal error metadata. They did not
pass differential qualification and are not included as passing corpus cases.
That existing reporting difference remains a follow-up; accepting unknown
telemetry does not bypass session validation.

No live model call, package release, merge, or deployment was performed. Fresh
remote CI and broader version/platform qualification remain separate.
