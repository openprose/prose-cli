# Native output-limit diagnostics

IMP-081 repairs a demonstrated diagnostic gap without changing the existing output limits. The outer runner continues to treat contracts and results as opaque inputs and native output.

When the next redacted JSONL record would exceed the native capture limit, both products return `HARNESS_FAILED` (exit 22) with `details.transportDiagnostic`:

```json
{
  "schema": "openprose.transport-diagnostic/1",
  "reason": "native-capture-limit",
  "observedBytes": 1050000,
  "limitBytes": 1048576,
  "saturated": false
}
```

The numbers above are illustrative. `observedBytes` counts the retained prefix plus the attempted record, serialized as UTF-8 after redaction, including its newline. The rejected record is not appended. Numeric fields saturate at 4,294,967,295 with `saturated: true`; they contain no native text or paths. The existing `aggregate-stdout-limit` reason counts raw stdout instead. Their byte counts can differ because redaction can expand or shrink a record. If several boundaries could fail, the reported cause identifies the boundary observed by the runner; it is not a reconstruction of missing output.

The existing `--native-output-bytes` option, default 64 MiB limit, configured bounds, capture privacy and cleanup remain unchanged. There is no retry, fallback, silent cap increase or successful truncation. Rust previously classified capture overflow as `INTERNAL_ERROR`; it now agrees with Bun that exceeding a configured native-output allowance is a harness failure. Other I/O failures retain their existing classification.

## Evidence

The originating financial n003 run retained a prefix near 64 MiB and ended without its complete assessment. Provider-free replay reproduced capture overflow and the loss of its reason in public Bun output. The actual first boundary in that live run remains unresolved because its next record was not retained. This repair does not turn that run into a successful assessment. [Retained research evidence](https://github.com/openprose/openprose-research/tree/06f42cc5/projects/context/experiments/financial-contracts-v1/native-anthropic-v1/phases/n003/coordinator-review).

The shared installed-adapter adversary emits synthetic native records through actual compiled Rust and Bun CLIs at the supported 1 MiB minimum. It separately checks capture overflow after redaction expansion and raw stdout overflow, the bounded diagnostic, private retained prefix, exit status, no fallback and settled harness PID. Before implementation, the capture cases fail: Bun omits the diagnostic and Rust returns exit 70. Both stdout cases already preserve their diagnostic. After implementation, all four product/condition cells pass. Shared UTF-8 boundary fixtures additionally check an exact accepted prefix and a first record that becomes oversized only after redaction.

The first test draft also expected an optional `saturated: false` field from existing stdout diagnostics and allowed a fake-harness BrokenPipe traceback. Those fixture errors were corrected before recording the baseline. A later comparison exposed the existing difference between Bun's all-environment redaction and Rust's narrower secret selection: locale `C` can expand an already-redacted placeholder in Bun. The diagnostic still reports actual post-redaction bytes correctly. The cross-product byte control explicitly uses a longer locale to isolate the capture-limit behavior. [IMP-056](https://github.com/openprose/openprose-workspace/blob/main/work/items/IMP-056.md) remains the separate redaction-policy task; this repair does not resolve it.

Focused local validation: 30 shared contract/schema tests, 16 Bun capture/framing tests, Bun type checking, three Rust capture tests, and the compiled cross-product four-cell control pass. Full source admission and remote candidate CI must pass before integration. No provider requests are made by this qualification.

Related: [native output fixture](../cli/shared/fixtures/adapters/native-output.v1.json), [transport diagnostic schema](../cli/shared/schemas/transport-diagnostic.schema.json), [installed-adapter adversary](../cli/conformance/adversarial/adapter-products/README.md), and [workspace task IMP-081](https://github.com/openprose/openprose-workspace/blob/main/work/items/IMP-081.md).
