# runs cases

Shared black-box cases for `cli run quote|submit|watch|input|cancel` (see
[`docs/service/runs.md`](../../../../../docs/service/runs.md) and
[`../README.md`](../README.md)). Each case's semantic oracle (exit code, problem
code, key details, event types, journal state via `journalAfter`) was asserted
before its exact output was captured from the Rust test-seam product; Bun must
match byte for byte. Inputs over 1 MiB cannot be corpus files and are pinned by
process tests in both products (`tests/service_runs.rs`, `test/service-runs.test.ts`).
