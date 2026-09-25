# Service contract data

Normative data for `prose cli` service operations. The behavior is specified
in [`cli/SPEC.md`](../../SPEC.md) (hosted service operations) and described
per command in [`docs/service/`](../../../docs/service/).

| File | Artifact | Author | Purpose |
| --- | --- | --- | --- |
| `operations.v1.json` | A2 | this repository | The operation manifest (`openprose.service-operations/1`). Both products embed it and drive parsing, help, confirmation and transport classes from it. `prose cli service operations --json` prints its public projection (`operations-public.v1.json`) as the envelope's `result`; `--output jsonl` prints one record line per operation and a page trailer. Each operation's `command` is its argv after `prose`, starting with `cli`. |
| `operations.schema.json` | | this repository | Schema of the manifest. |
| `operations-public.v1.json` | | this repository | The public projection of the manifest: the allowlist of members `cli service operations` prints (and the capabilities document's manifest digest covers), its size budget (`maxBytes`) and strings that must never appear in it (`forbid`). Routes, catalog entries, service error strings, the service origin, the key format and the client's own settings stay internal. `service_operations.py --validate` checks the budget and the forbidden strings. |
| `guide.v1.md` | | this repository | The agent guide `prose cli service guide` prints byte for byte; `--json` returns its `## ` sections. Both products embed it. Edit it by hand, then run `python3 cli/ci/render_service_help.py --write` to regenerate the corpus cases that pin it; `--check` parses every `prose cli ...` command in it against the manifest. |
| `help.v1.json` | | generated | Exact `--help` text per command topic, the `cli service capabilities` document (`capabilities`) and the human pages of `cli service capabilities` and `cli service operations` (`views`), rendered from the manifest by `cli/ci/render_service_help.py`. Never edit by hand. |
| `service-interactions.v1.json` | A1 | the service | Vendored service interaction export, projected to what the CLI uses: each mapped interaction's id, principal, effect, reversibility, agent policy and the routes the manifest sends. Nothing else from the export is vendored. |
| `service-interactions.source.json` | | generated | A1 provenance: SHA-256 of the export file and of the projection. |
| `responses/*.schema.json`, `responses/index.json` | A3 | this repository | Acceptance schemas for raw service responses. They require what the CLI consumes and tolerate extra fields. `x-probed: false` marks shapes not yet observed on the hosted service. |

Closed per-operation result projections live in
[`../schemas/service/`](../schemas/service/); the envelopes are
`../schemas/service-operation.schema.json` and `../schemas/service-event.schema.json`.

## Changing the contract

- **Re-vendor A1** with `python3 cli/ci/sync_service_interactions.py --from <export file>`, where the file is the service's interaction export. Only maintainers of the OpenProse service can obtain the export, so re-vendoring is a maintainer task; contributors work against the vendored copy, and `--check` without `--from` needs no export (the `shared-contracts` and `service-coverage` gates verify the vendored digests).
  The script checks the public format first: the public fields present, public principals only, and sorted, unique ids; then it writes the projection of the interactions the manifest maps and the routes it sends (public fields only), and prints (without recording) the exported interactions the CLI does not map. Then update `interactionsSource.sha256` in the manifest on purpose. Mapping a new interaction is a manifest edit followed by a re-vendor.
- **Edit the manifest** directly, run `python3 cli/ci/render_service_help.py --write`, then run the gates `service-operations-corpus`, `service-coverage`, `service-help` and `shared-contracts`.
- **Compatibility.** Manifest, schema, taxonomy and fixture-format changes are additive; a change that alters an existing document shape needs a new schema version.

## Manifest invariants (checked)

- **Mapping.** Every vendored A1 interaction is mapped by an operation, and every vendored route is sent by one. The vendored export holds nothing else.
- **Requests.** Each request names its catalog route exactly (method, path, auth). Its path matches the catalog template: a trailing `{path}` catalog parameter also matches zero or more segments. A route whose catalog auth requires a key is sent with the bearer credential.
- **Confirmation.** `confirm` is true whenever any non-GET/HEAD request's interaction has agent policy `confirm` or `never`, is `DELETE`, has effect `money`, `outward` or `destructive`, or is not reversible. Only the frozen operations may record a `confirmWaiver` instead.
- **Effect.** An operation's effect is at least the catalog effect of each of its non-GET/HEAD requests.
- **No cost.** No result or response schema declares a property matching `/cost/i`. Open maps exclude such names with `propertyNames`.
- **Exit codes.** Every operation's `exitCodes` is ascending and starts with `0 success`. Each listed code has that exit in `shared/errors/taxonomy.v1.json`, and exits 20 and above list their codes. A code that only a route override produces (`RUN_SUBMISSION_AMBIGUOUS` on `POST /run`) is listed only by an operation that sends that request. Every exit or code that a shared corpus case expects from the operation is listed, and every listed code except `CANCELLED` is expected by at least one of its cases. The help's "Exit codes:" line is rendered from this list (`render_service_help.py --check`).
- **Exit dictionary.** The top-level `exitCodes` maps every error code a service operation can end with to `{exit, retryable, meaning}`, equal to the taxonomy entry. It contains every code a shared corpus case emits (with that case's exit), every code an operation's `exitCodes` lists and every error-classification target; any other entry needs a reason in `UNCASED_DICTIONARY_CODES` (`render_service_help.py`).
- **Examples.** Every operation has `examples`: `prose ...` lines naming its command path with only its options, every required option and argument, and `--yes` or `--preview` when it is confirm-class. The `service-operations-*` gates also run each one and fail when the product's grammar parser rejects it.
- **Guide.** `guide.v1.md` starts with its `# ` title, has the required `## ` sections (grammar, credentials, confirm and preview, exit codes with resume and detach, paging, getting a run's answer, recipes) and the three recipes, no `## ` line inside a code fence, and is linked from `docs/hosted-service-client.md`. Every `prose ...` line in its `sh` fences and inline code parses against the manifest like an example, and every bare `` `cli ...` `` span names a command path or group (`render_service_help.py --check`). The `service-operations-*` gates also run each non-template line through the product's parser.
- **Intent inference.** Every `grammar.intentInference` noun synonym names a real command path, every verb synonym resolves in at least one group and never shadows a verb there, and every option alias names a real option. Adding a synonym is a manifest edit plus a `framework/` corpus case; neither port changes.
