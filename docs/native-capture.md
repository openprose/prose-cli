# Native evidence capture

`--native-log /absolute/new/file.jsonl` opts into a private native-event artifact for the same execution. The parent directory must exist and the file must not already exist. Both runners create it with mode 0600 on Unix, append JSON records before protocol admission on Unix, and stop the run if the capture exceeds 64 MiB or cannot be written. `native_log` and `PROSE_NATIVE_LOG` also select this path. Default is off.

Capture records are parsed native JSON events, not byte-for-byte stdout; malformed JSON bytes cannot be represented and are not captured. On Unix, successfully parsed values rejected by protocol admission are captured, including out-of-order records. Invalid JSON, partial records, and records exceeding framing limits remain outside capture; this is not a complete raw-stdout archive. The Rust Windows host path currently captures only admitted records; rejected-record coverage is not claimed there. Bun uses its pre-admission callback. Rust protocol failures expose a closed reason code and admitted-record count without echoing rejected content. The artifact may end without native completion after a failure. Native protocol validation still decides whether the process settles. Capturing an event does not admit it or establish program fulfillment.

Known selected credential values and runner control secrets are replaced in string values. This is not a general redaction guarantee: tools may read sensitive files, tool arguments/results may contain other secrets, and split or encoded values may escape replacement. Treat this as a private sensitive artifact. Use synthetic fixtures for shared evidence. No capture file is uploaded automatically.


## Capture-secret selection correction (IMP-056)

Capture replaces selected credential values, runner recursion/nonce secrets and the private credential-config path when one is created. Ordinary environment values such as USER, LANG and HOME are not capture secrets. Console/public-output suppression is a separate policy and remains unchanged. This distinction matters for evidence: replacing an ordinary user name such as `mm` can corrupt `command_execution`, and replacing locale `C` can alter the inserted `[REDACTED]` marker. Bun previously used its broader public-output list for capture; the correction aligns it with the documented boundary and Rust's existing behavior.

Shared installed-product controls exercise both a normal selected key and a key whose literal value matches an event identifier. Ordinary short environment values must leave the private capture intact; actual secret matches must still be replaced, including in structural strings. Therefore this correction does not promise universally replayable event identities, and it does not exempt structural values from privacy. The broader trace-identity design in workspace IMP-056 remains open. Existing capture format, permissions, size limits, failure handling and retention remain unchanged.


Focused qualification: 137 Bun installed-adapter/capture tests and type checking pass. The compiled Rust/Bun shared control passes both selected-key conditions, and both products still pass the distinct capture/stdout limit controls. Before the correction only Bun fails the new capture-selection cases; Rust needs no implementation change. Full source admission and remote candidate CI remain integration gates.
