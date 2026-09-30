# Local integration qualification — September 30, 2026

This machine-local integration rehearsal combines corrective hosted commit
`aa75596` with corrective Claude commit `c331e74`. It has not been pushed or
merged in GitHub. The Rust adapter conflict preserves PR 11's session-bound
unknown-event compatibility and task-lifecycle assertions; formatting is applied
only to that leased source file. Both ownership records are retained.

On macOS ARM64 the combined source passes the two new shared Claude cases
against freshly compiled Rust and Bun products with clean differential output;
143 focused Bun adapter/proxy tests (one recorded-stream test skipped); Bun
type checking; 28 contract tests; 10 focused Rust Claude tests (one ignored);
and 98 Rust service tests. These combined checks supplement the separate
candidate evidence, which includes 1,089 hosted cases and 21 registry cases
against each compiled product. The full hosted corpus has not been repeated
against this combined source.

The separate qualification records retain the baseline full-admission and
adapter-oracle failures. Remote CI and CodeQL have not been rerun or cleared.
No live service or model call, release or deployment was performed. This local
merge commit is a reviewable integration candidate, not deployment approval.
