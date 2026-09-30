# Jobs

A **job** (the service calls it a *trigger*) starts runs of a pinned program on
its own: on a schedule, or when a webhook receives an event. The commands are service
operations and behave the same in the Rust and Bun builds. Every command needs
an API key.

| Command | Request | `--yes` |
| --- | --- | --- |
| `prose cli job list` | `GET /triggers` | no |
| `prose cli job show JOB_ID` | `GET /triggers/{id}` | no |
| `prose cli job create --spec-file FILE\|-` | `POST /triggers` (plan: `GET /run/quote`) | yes (money) |
| `prose cli job update JOB_ID --spec-file FILE\|-` | `PUT /triggers/{id}` | yes |
| `prose cli job configure JOB_ID --config-file FILE\|-` | `PUT /triggers/{id}/configuration` | yes |
| `prose cli job delete JOB_ID` | `DELETE /triggers/{id}` | yes |
| `prose cli job deliveries JOB_ID` | `GET /triggers/{id}/deliveries` | no |
| `prose cli job rotate-secret JOB_ID` | `POST /triggers/{id}/rotate-secret` | yes |
| `prose cli job contract list JOB_ID` | `GET /triggers/{id}/contracts` | no |
| `prose cli job contract attach JOB_ID OWNER/SLUG@REV` | `POST /triggers/{id}/contracts` | yes |
| `prose cli job contract detach JOB_ID OWNER/SLUG@REV` | `DELETE /triggers/{id}/contracts/{ref}` | yes |

Without `--yes`, a confirm-class command sends nothing and exits 2 with
`CONFIRMATION_REQUIRED`, `details.reason` saying what `--yes` would do (for
`rotate-secret`: the current signing secret stops working now; for `contract
detach`: the job stops running that program), the planned request (method, path,
body digest and the non-secret `summary`: `type`, `name`, `programRef`,
`interval_seconds`, `delivery_mode`, `inputKeys`) and copyable
`confirmArgv`/`previewArgv`. `--preview` prints the same plan and exits
0. `job deliveries` and `job rotate-secret` need webhook jobs; where they are not
available for the account they fail with `SERVICE_FEATURE_DISABLED`.

## Creating a job

The spec is a JSON object of at most 64 KiB, from a file or standard input.
Before any request the CLI checks that it is UTF-8 JSON and an object, then
checks it against the closed per-type schema published as the operation's
`spec` in `prose cli service operations --json`: `type` is
required and must be a known type (a typo names the nearest); each type
accepts only its own keys plus `type`, `environment`, `inputs` and `files`
(an unknown key names the nearest accepted one, compared without case, `_`
and `-`); required keys must be present; `program_ref` must be pinned
(`OWNER/SLUG@REV`); `inputs` values must be strings; `interval_seconds` is an
integer from 60 to 2,678,400 (31 days). A camelCase key such as
`intervalSeconds` is refused with its snake_case name. Every problem is
reported at once: `details.violations` lists them and the reason numbers
them. The bytes are then sent unchanged; the
service validates everything else and a 400 carries its message in
`details.serviceMessage`. `job update` checks its closed key set (webhook
settings, or `url`, `interval_seconds` and `mode` for a website-change job) and
refuses an empty spec; `job configure --config-file` checks the configuration,
each `bindings[]` entry and its `run_configuration` the same way. A spec key
given as an option (`job update JOB_ID --name x --yes`) exits 2 and suggests
`--spec-file -`, with the JSON object to pipe in (`{"name":"x"}`) as
`details.suggestedStdin`; `*_secret` keys are never moved into the
suggestion. `job create spec.json` suggests `--spec-file spec.json` when the
file exists.

```sh
# A schedule: runs a pinned program every day.
printf '%s' '{"type":"schedule","program_ref":"me/digest@0123456789abcdef","interval_seconds":86400}' \
  | prose cli job create --spec-file - --preview --json
prose cli job create --spec-file schedule.json --yes --json

# A webhook in test mode (receives events, never starts runs).
printf '%s' '{"type":"webhook","delivery_mode":"test","name":"my-hook"}' \
  | prose cli job create --spec-file - --yes --json
```

- **A new schedule starts its first run about one second after it is
  created**, then every `interval_seconds`. That is why `job create` is a money
  operation: its plan (`--preview` or `CONFIRMATION_REQUIRED`) includes the
  service's hold quote (`plannedRequest.quote`, from the anonymous
  `GET /run/quote` for the default environment; a flat hold, independent of
  program and model). With `--yes` no quote is read. A webhook with no
  `program_ref` starts no runs: its plan has effect `write`, no quote is read
  and no hold is shown.
- `prose cli job create --help` prints minimal schedule and webhook specs and
  their optional keys. The service's job types are listed by `job list`
  (`types[].config_fields`). A spec without `type` is refused locally
  (`INVOCATION_INVALID`), because the service would default it to a website
  monitor.
- `program_ref` is `OWNER/SLUG@REV` where REV is the program's `rev_id` (the
  `program.rev_id` of `cli program save` or `cli program show`), not its
  `commit_id`; a commit id answers "That contract revision was not found."
- Schedule spec keys: `type`, `program_ref`, `interval_seconds`, and optionally
  `model`, `reasoning_effort`, `environment`, `inputs` (name → string), `files`,
  `repository_url`, `repository_branch`, `output`. Webhook spec keys: `type`,
  and optionally `name`, `program_ref`, `delivery_mode` (`test` or `live`),
  `receiver`, `receiver_secret`, `reply`, `reply_secret`.
- A webhook `create` result carries `endpoint` and `signing_secret`. **They
  appear only in that result** (and in `rotate-secret`), never in `job show` or
  stderr. Human mode prints them once on stdout and a warning on stderr. Both
  results also carry `endpoint_url`, the absolute URL to configure in the
  sender (the service origin + `endpoint`).

## Reading jobs

`job list` returns `{jobs, max_jobs, job_limit, types}` (each type
`{id, label, description, config_fields}`); an empty account is exit 0 with
`jobs: []`. `job show` returns `{job, status}`. Every job field is built from
its public field list, in snake_case (`interval_seconds`, `created_at`,
`next_fire_at`, `last_run_id`, `program_ref`, …); any other service field
(internal references, adoption and driver detail, repository ids and detail
objects, receiver and reply details) is dropped, as is the webhook's relative
inbound endpoint; when that endpoint is the secret-free job-id path,
`job show` reports it as the absolute `endpoint_url`. `status.counts` reports
the job's runs in the public states `queued`, `running`, `completed`, `failed`
(including a run whose outcome is unknown), `cancelled` and
`awaiting_billing`; human output prints the nonzero ones, the last as
`awaiting billing settlement N`. Every epoch-ms time (`created_at`,
`next_fire_at`, `last_fired_at`, `last_event_at`, `secret_rotated_at`,
deliveries' `received_at`) also appears as `<name>_iso` (RFC 3339 UTC, null
when the time is null); a delivery's `runs` lists the runs it started. Human `job show|create|update|
configure` print one `label: value` line per field with RFC 3339 times and end
with copyable `Next:` commands (`job configure ...
--interval-seconds N --yes` for a schedule, `job deliveries` and `job contract
list` for a webhook, `run show` of the last run); an empty `job list` says
`No jobs.`.
Free service text (`name`, `last_error`, descriptions) has control characters
replaced by spaces; identifiers are checked exactly, and a malformed one is
`SERVICE_PROTOCOL_INVALID` with `details.reason` naming the field.

## Changing a job

- `job update JOB_ID --spec-file F --yes` sends a partial change. For a webhook
  the service accepts `name`, `receiver`, `receiver_secret`, `delivery_mode`,
  `reply` and `reply_secret`. Schedules are refused (405 →
  `SERVICE_REQUEST_REJECTED`); change them with `job configure`.
- `job configure JOB_ID --config-file F --yes` replaces a schedule's cadence and
  run configuration. The configuration needs `interval_seconds`,
  `configuration_revision`, `revision_token` and `bindings` (every contract,
  each `{program_ref, run_configuration}`), under the names `job show --json`
  prints them: `status.configuration_revision`, the opaque
  `status.revision_token`, and each `status.contracts[]` entry's
  `program_ref` and `run_configuration`. Send them back unchanged apart from
  what you mean to change; the service replaces every binding, so an input or
  file you leave out is deleted. A stale `revision_token` is
  `SERVICE_WRITE_CONFLICT` (reload with `job show` and retry). The CLI
  refuses camelCase names locally with the request name
  (`INVOCATION_INVALID`) and translates the configuration to the service's
  own names before sending it.
- **Inputs named like a cost.** No result property name may match /cost/i
  (the money rule), so an input such as `cost_center` or `max_cost` is not
  a key of `run_configuration.inputs`; `job show` lists it, with its value, in
  `run_configuration.input_entries` as `{name, value}` (ordered by name).
  Before `job configure`, move each entry into `run_configuration.inputs` as
  `"name": "value"` and remove `input_entries`; a body that still contains
  it is refused locally, so such an input is never deleted silently.

```sh
# show -> configure round trip (jq): cadence to 2 days, everything else kept.
prose cli job show "$JOB" --json | jq '.result.status | {
  interval_seconds: 172800,
  configuration_revision: .configuration_revision,
  revision_token: .revision_token,
  bindings: [.contracts[] | {program_ref: .program_ref,
    run_configuration: (.run_configuration
      | .inputs = ((.inputs // {}) + ((.input_entries // []) | map({(.name): .value}) | add // {}))
      | del(.input_entries))}]}' \
  | prose cli job configure "$JOB" --config-file - --yes --json
```
- **While an occurrence of the schedule is running**, including the first one
  that starts right after `job create`, the service answers 500 and the CLI
  reports `SERVICE_UNAVAILABLE` (retryable). Wait and retry; `job show` reports
  the latest run in `status.last_run_id`.
- `job contract attach|detach` take a pinned `OWNER/SLUG@REV`
  (`prose cli program show OWNER/SLUG --json` prints the latest as `ref`; a
  revision number such as `@1` exits 2 naming `program show OWNER/SLUG@1 --json`,
  which resolves it for your own program). Their result
  is `{contracts: [{program_ref}]}`: the one contract the service attached or
  detached. Run `job contract list` for the full list. `--model` is refused
  because the service ignores a model on attach; attached contracts run on the
  service's default job model. Schedules change contracts only through
  `job configure` (the contract routes answer 405 →
  `SERVICE_REQUEST_REJECTED`).
- `job contract list` reports each contract's `program_ref`, `program_slug`,
  `enabled` and `model` and drops the program text and configured inputs.

## Secrets

`job rotate-secret JOB_ID --yes` returns `{id, endpoint, endpoint_url, signing_secret}` with
the new secret; the old one stops working. It is a webhook-only operation: a
schedule answers 405 (`SERVICE_REQUEST_REJECTED`).

## Deleting

`job delete JOB_ID --yes` returns `{id, deleted: true}`. `JOB_ID` must be a
lowercase UUID as `job list` prints it; anything else (`job_nope`, `..`) exits
2 `INVOCATION_INVALID` before any request, with `suggestedArgv` `cli job list`
. Deleting an unknown or already-deleted job with a well-formed
id exits 0 with `{id, deleted: true, already_absent: true}`, so
a retry after a lost response is safe; human output says nothing was
deleted. Any other job 404 is `SERVICE_RESOURCE_NOT_FOUND` with
`details.resource` `{kind: "job", id}` and `details.suggestedArgv` `cli job
list`. A running occurrence of a deleted schedule is allowed to finish.
