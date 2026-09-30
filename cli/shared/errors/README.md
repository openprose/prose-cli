# Stable runner errors

`taxonomy.v1.json` freezes the v1 code, boundary, portable exit code, default
message, and single corrective action shared by both runners. Implementations
may attach sanitized mechanical details, but they do not paraphrase the
corrective action in machine output.

Messages name the failed boundary. Actions are safe to show to agents and
people and never contain discovered secrets. A language semantic failure is
not a runner error and therefore is not listed here; its portable exit code is
30 as defined by the result contract.

The hosted service operations added fifteen codes (27 to 42). The
additions are `CONFIRMATION_REQUIRED` (exit 2), ten service codes with exit 10,
`SERVICE_WATCH_DEADLINE` (21), `HOSTED_RUN_DETACHED` (21, not retryable),
`HOSTED_RUN_FAILED` (22), `RUN_SUBMISSION_AMBIGUOUS` (22) and
`HOSTED_RUN_CANCELLED` (24, not retryable). Existing entries, including the pinned
`HOSTED_UNAVAILABLE` text, are unchanged. Service responses are classified by
body `code`, then route override, then HTTP status, as declared in
`shared/service/operations.v1.json` (`errorClassification`).
