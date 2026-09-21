# IMP-034 staging account candidate validation

This record covers explicit public CLI staging authentication and organization listing. It does not establish registry publication, hosted execution, production readiness, or public release availability.

## Passing checks

- Shared process corpus: 26 cases passed independently against the Rust test-seam executable and the compiled Bun test-seam executable.
- Shared result schemas: Ajv validated all 26 expected result shapes and rejected 26 additional secret-field properties.
- Bun 1.3.5: typecheck passed; 35 focused parser, service and help tests passed.
- Rust: seven staging unit tests, fifteen parser tests, taxonomy parity, and the service-token harness/probe isolation regression passed. Default and test-seam builds passed using cached dependencies.
- Ordinary Rust and Bun binaries ignored the test fixture variable in a no-network invocation. No test fixture may select an alternate service in an ordinary build.
- Direct `cli/ci/check_architecture.py` and Git whitespace validation passed.
- Independent Astra review identified and prompted fixes for credential reflection, inconsistent response predicates, malformed fixtures, HTTP error classification, cancellation precedence, and device expiry handling.

## Reproduction

Use the pinned tools and dependency setup in `cli/CONTRIBUTING.md`. The normal local admission runner includes `staging-service-corpus`, `staging-service-rust-build`, `staging-service-rust`, `staging-service-bun-build`, and `staging-service-bun` gates. Run the build gates before their corresponding process gates when selecting individual gates.

The standalone corpus runner accepts a compiled test executable:

```sh
python3 cli/conformance/runner/staging_service.py --validate
python3 cli/conformance/runner/staging_service.py -- /absolute/path/to/test-seam/prose
```

Every case uses an isolated home directory and a closed fake service transcript without live credentials. Fixture handling is compiled out of ordinary builds.

## Limits and outstanding qualification

No live account login, OS credential-store mutation, or authenticated staging request was performed for this CLI candidate. The browser approval flow and cross-implementation native credential-store interoperability still need explicit live qualification.

Rust local storage currently supports macOS; other platforms fail closed and may use the scoped staging environment credential. Bun delegates to its operating-system credential API. Native storage operations can request OS approval. A timed-out operation that cannot be cancelled may finish later; status should be checked before retrying.

After unrestricted access was restored, the full Bun 1.3.5 suite passed: 582 passed, two skipped, zero failed. The full Rust 1.87.0 workspace/all-targets suite with test seams passed: 331 passed, two ignored, zero failed. Shared Python contract tests passed 27/27, architecture boundary unit tests passed 34/34, and the portable Windows host tests passed 17/17 on macOS (this is not native Windows qualification).

Diagnosis distinguished four causes:

- Two Bun output assertions and the equivalent Rust assertion rejected any `/tmp/` path, including the deliberately displayed runner executable. The Bun failures reproduced on unchanged baseline `eb40bc4`; the Rust assertion was also unchanged. The correction exempts only the exact runner invocation and retains rejection of other temporary paths.
- The new staging taxonomy was missing from a shared test's explicit expected-code set. This candidate regression was corrected; the test still checks the exact set.
- The locked Python wheels target Python 3.10. Installation under Python 3.11 failed hash verification; Python 3.10.20 installed the unchanged lock successfully. Contributor instructions now specify that requirement.
- The protected package manifest still named the old `prose.git` repository, unlike the current packager. Correcting it to `prose-cli.git` restored all 14 draft-release tests.

The broader historical architecture/release suite failed identically on the unchanged baseline: 156 tests, two failures and 48 errors. Its missing legacy release workflows are explicitly documented in `docs/cli-distribution.md`; restoring their release authority is a separate migration, not an authentication fix. Three documented contributor issue forms are restored, and support links now target this repository. A historical blocker snapshot now uses a controlled fixture rather than asserting stale facts about the current checkout. The affected suites pass: 22 public-documentation tests, 19 contributor-documentation tests, and 14 draft-release tests. Full admission must not be described as passing while those historical release checks remain unresolved.

Rust formatting of the candidate's three newly affected files is corrected. Fifteen pre-existing files still fail the full formatting check, and five initial Clippy diagnostics in unchanged supervisor/framing code remain. These are separate baseline qualification debt; supported-platform CI and live credential-store qualification remain outstanding.

No model-provider calls, paid runs, backend edits, deployment or publication were performed.

## Persistent environment extension

Production-default account routing and user-only persistent staging selection are implemented in both ports. Shared process checks pass three multi-invocation persistence sequences and all 26 staging cases against both test binaries. Pinned Bun 1.3.5 passes 40 focused environment/account/parser tests and typecheck. Rust focused service/config/process tests pass. Current restricted execution again prevents socket-based full-suite qualification; earlier full-suite results above apply to the preceding candidate, not this extension. A broad agent run used non-pinned Bun 1.4.2 and reported socket failures, a Python warning assertion and a build digest mismatch; it is not pinned-tool qualification evidence. Live login remains unverified.
