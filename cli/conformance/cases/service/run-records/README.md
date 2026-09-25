# run-records cases

`cli run list|show|download|share`. Behavior is described in
[docs/service/run-records.md](../../../../../docs/service/run-records.md); the
case format is in [`../README.md`](../README.md).

The cases cover: default and explicit paging (`limit`, `before`,
`nextBefore`), empty pages, local `--limit` validation, the unknown-cursor 400,
strict projection (dropping `file_urls`, `customer_id` and unknown fields;
`files` as an array of paths), `--file` inline text, binary, encoded paths and
`--output-file` (never overwriting, removed on failure), download trees with
the `.prose-run-manifest.json` marker written last, refusal of existing or
parentless destinations before any request, unsafe or conflicting manifest
paths (destination never created), partial downloads without a marker, and
`run share` confirmation, preview, result and human warning.

Not expressible here, and pinned by process tests instead
(`cli/rust/crates/prose-cli/tests/service_run_records.rs`,
`cli/bun/test/service-run-records.test.ts`): the 64 MiB per-file and 512 MiB
per-run caps (lowered through the test-seam `PROSE_TEST_SERVICE_DOWNLOAD_LIMITS`)
and the 1 MiB inline `run show --file` cap (fixtures over 1 MiB cannot be
committed). JSONL output of these non-stream operations (one envelope line) is
not a case because the corpus validator reads every JSONL line as a
`service-event/1` line.

The expected outputs were recorded from one product, reviewed by hand, and
must be matched byte for byte by the other (authoring helper kept outside the
repository).
