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

The full Bun suite reported 556 passes, two skips and 25 failures in this execution environment: socket permission failures, a FIFO-related subprocess warning, and temporary-path output assertions. An initial run with incomplete PATH had additional failures; the counts above are from the corrected PATH run. An exact baseline comparison remains outstanding. The broader Rust suite also had socket permission and temporary-path failures. These results are not a claim that the full suites passed or that every failure is unrelated.

The broad architecture unit gate also failed; the direct architecture checker passed. Python JSON Schema tests were unavailable because the required Python dependency could not be downloaded; the Ajv checks are recorded separately. Rustfmt and Clippy were not installed. Full normal admission and supported-platform CI remain required before merge/release.

No model-provider calls, paid runs, backend edits, deployment or publication were performed.
