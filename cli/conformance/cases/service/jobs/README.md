# jobs cases

Shared black-box cases for `prose cli job …` (see
[`docs/service/jobs.md`](../../../../../docs/service/jobs.md) and
[`../README.md`](../README.md)). Rust and Bun must produce the same documents.

They pin: the closed trigger/status/type projections (camelCase kept, internal references,
receiver/reply details and the `job show` endpoint dropped, free text
sanitized); the schedule configuration fields `job configure` needs, with
inputs named like /cost/i kept losslessly in `inputEntries` and the
show → configure round trip that keeps them; local spec checks that send
nothing (non-object, invalid JSON, > 64 KiB, missing file, `interval_seconds`
outside 60..2,678,400, camelCase `intervalSeconds`, a spec without `type`,
schedule without an interval, configure without an interval, configure bodies
with result-only camelCase names or unmerged `inputEntries`); the `job create` plan with
the hold quote; the webhook secret only in the create/rotate result with a
stderr warning in human mode; confirmation for every mutation; pinned contract
references and the refused `--model`; feature-disabled, not-found, conflict,
rejected and unavailable classification.
