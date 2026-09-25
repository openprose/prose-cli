# Shared runner schemas

These Draft 2020-12 JSON Schemas are the normative data contracts for the
outer OpenProse runners. They deliberately do not define OpenProse source,
task-envelope semantics, or the language-owned terminal envelope.

All objects are closed unless a field is explicitly documented as opaque.
Opaque values are transported and hashed, never interpreted by the runner.
Schema identifiers are stable HTTPS identifiers; local file names are the
canonical repository copies.

Run the contract checks from the repository root with:

```sh
python3 cli/shared/tests/test_contracts.py
```

The test dependency is pinned in `../requirements-test.txt`.


Machine output names no service environment: a developer build whose origin
was overridden at run time says so only in its human output.
`package-operation.schema.json` describes the results of publish, fetch, list
and withdraw, which `cli package ...` prints in the service-operation envelope;
`service-account.schema.json` (`authenticated`, `credentialSource`) and
`organization-list.schema.json` (`organizations`) are the results of
`cli auth ...` and `cli org list`. Package results contain validated publication
receipts or one public listing page, never local destination paths or credentials.
The bounded listing cursor is opaque to callers and at most 210 ASCII characters.

Package envelope, canonical bytes, hashes, and receipts are defined by
[`../fixtures/registry/FORMAT.md`](../fixtures/registry/FORMAT.md) and its checked-in
vectors. A schema match alone does not establish artifact integrity: fetch also
requires hash, canonical-byte, reference, visibility, and inventory agreement.
Directory authoring manifests reject unknown and duplicate keys before upload.
These contracts do not establish backend deployment or permission to publish.

## Service operations

`service-operation.schema.json` (`openprose.service-operation/1`) is the JSON
result of every `cli` command: `schema`, `operation` (the CLI operation id),
`interaction` (the service catalog id, or null), `result` (a paged
operation's result carries `nextBefore`) and `problem`. It is also the JSON
shape of every invocation error of a service command line: `operation` is then
`cli` when the argv names no operation; the schema allows that only with a
problem.
`service-event.schema.json`
(`openprose.service-event/1`) is one JSONL stream line: projected `status`,
`agent_activity`, `text_chunk`, `browser_live_view_changed`,
`history_truncated`, `error` and `unrecognized` events, then exactly one
`service.completed`, `service.failed` or `service.detached` line carrying the
envelope (`service.detached` exactly when the problem's exit is 21: the run
continues).
`service-record.schema.json` (`openprose.service-record/1`) and
`service-page.schema.json` (`openprose.service-page/1`) are the `--output jsonl`
lines of a list operation (the manifest's `output.records`): one record line per
item, then one page trailer with `count`, `nextBefore` and `meta`.
Every epoch-ms result field `X` has an additive `X_iso` sibling (RFC 3339 UTC or
null).
`service-capabilities.schema.json` (`openprose.service-capabilities/1`) is the
result of `prose cli service capabilities --json`: grammar, environments, the
exit dictionary keyed by error code, environment variables, the noun/verb index
and every operation with its examples.

The closed per-operation results are `$defs` in `service/<feature>.schema.json`,
named by each operation's `output.schema` in
[`../service/operations.v1.json`](../service/operations.v1.json). They keep
service field names verbatim (snake_case; camelCase for jobs) and drop every
field the projection does not name. Signed `file_urls`, `customer_id`, webhook
endpoints and secrets are never projected, except the secret-bearing field of
the one operation that creates it (and a webhook's `endpointUrl` in `job show`
when its endpoint is the secret-free job-id path). No property name may match `/cost/i`; open
maps exclude such names with `propertyNames`. Run the checks with
`python3 -m unittest cli/shared/tests/test_service_contract.py`.

`runner-error` details gain the typed keys `serviceStatus`, `serviceCode` (a
closed allowlist), `serviceMessage`, `runId`,
`afterSequence`, `remote`, `session`, `idempotencyKey` and `plannedRequest`.
As before, details remain open to other sanitized mechanical keys.
