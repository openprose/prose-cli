# Native output budget

`--native-output-bytes 134217728` raises the native aggregate stdout and capture allowances to 128 MiB. Use with `--output-contract native`. It is a runner limit, not a model token limit or instruction to change the program.

The omitted default remains 67,108,864 bytes (64 MiB). Explicit values are strict decimal integer bytes from 1,048,576 through 268,435,456 inclusive; no unit suffixes. Quoted TOML `native_output_bytes="134217728"` and environment `PROSE_NATIVE_OUTPUT_BYTES=134217728` follow normal flag > environment > project > user precedence. Other output modes reject an explicit selection.

The allowance applies independently to raw aggregate child stdout and, when requested with `--native-log`, serialized redacted native capture. It is not one shared pool. Serialization/redaction can change size, so either ceiling can fail first. Limits and capture enablement are exposed as `nativeOutputLimits` in native invocation, dry-run and result records; configuration explanation retains the selection's source.

Single records remain limited to 1 MiB; stderr, queues, image sizes and native framing constraints retain their existing limits. Reaching a ceiling still fails and settles the process; it never silently drops output, retries, increases the allowance or synthesizes completion. Larger allowances permit additional memory/disk use and are not a guarantee of completion or exact resident-memory bounds.

Native doctor JSON, dry-run and run receipts expose the fixed 1,048,576-byte record limit as `nativeOutputLimits.maxRecordBytes`, separately from `maxAggregateStdoutBytes` and `maxNativeCaptureBytes`. A `record-byte-limit` transport diagnostic reports `HARNESS_FAILED` with exit 22 and resource-specific recovery advice; it does not establish malformed JSON or an unsupported harness. `observedBytes` is the size detected before rejection, not the complete eventual record size. Return bounded tool summaries and put bulk data in artifacts. There is no per-record setting: raising `--native-output-bytes` cannot solve this limit. Reconcile any partial effects before another attempt; the CLI does not retry automatically.

Windows stdout validation supports the same maximum in the updated bundled host; stderr validation remains unchanged. Older hosts can reject an explicitly larger request. Actual Windows runtime qualification is not established by source/schema and provider-free tests on another OS.
