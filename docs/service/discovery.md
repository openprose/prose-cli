# Discovery: service status and triage, models, examples, repositories

Read-only commands that tell an agent what the OpenProse service offers before
it spends anything.
None of them needs `--yes`, none writes to the service, and an empty listing is
success (`[]`, exit 0). Results are closed projections
(`cli/shared/schemas/service/discovery.schema.json`); fields the service adds
later are dropped until the schema admits them.

| Command | Requests | Key | Result |
| --- | --- | --- | --- |
| `cli service status` | `GET /health` | none (anonymous) | `serviceStatus` |
| `cli service triage` | `GET /health`, then with a key `GET /wallet/balance`, `GET /organizations/default`, `GET /runs?limit=5`, `GET /triggers` | optional | `serviceTriage` |
| `cli model list` | `GET /models` | required | `modelList` |
| `cli example list` | `GET /examples/private` | required | `exampleList` |
| `cli example show <NAME> [--output-file F]` | `GET /examples/private`, then the example's same-origin source | required | `exampleShow` |
| `cli repo list` | `GET /repos` | required, plus a linked GitHub account | `repoList` |

Three more commands describe the CLI itself and send nothing:
`cli service capabilities [--json]` (grammar, environment variables, the exit code of every error code and every command with examples;
`openprose.service-capabilities/1`), `cli service operations` (a summary
table; `--json` is the full manifest, `--output jsonl` one command per line)
and `cli service guide [--json]`, the workflow guide: which key a command uses, `--yes` and `--preview`, exits with resume and detach, paging,
getting a run's answer, and recipes. Human mode prints
[`cli/shared/service/guide.v1.md`](../../cli/shared/service/guide.v1.md) byte
for byte; `--json` returns its `## ` sections as
`{"sections":[{"id","title","body"}]}` in the service-operation envelope
(`framework.schema.json#/$defs/guide`). `prose cli guide` suggests the command.

```sh
prose cli service triage --json
prose cli service status --output json
prose cli model list --output json
prose cli example show private-example --output-file example.prose.md
prose cli repo list --output json
```

**Proxies.** Both builds send service requests through `https_proxy` or
`HTTPS_PROXY` (for an `http` origin, `http_proxy` or `HTTP_PROXY`); the first
non-empty one wins. `no_proxy` or `NO_PROXY` lists hosts to reach directly: a
comma list where `*` means every host, a leading `.` is ignored, and an entry
covers its subdomains. `ALL_PROXY` and SOCKS proxies are not used. A proxy that
cannot be reached fails the command with `SERVICE_UNAVAILABLE`; the request is
never sent directly instead. The rules and their vectors are
`cli/shared/fixtures/transport/proxy-selection.json`. Certificate
authorities still differ: the Rust build trusts only its built-in roots (it
ignores `SSL_CERT_FILE`), while the Bun runtime also reads
`NODE_EXTRA_CA_CERTS`.

`--preview` and a `CONFIRMATION_REQUIRED`
plan for a paid command read the current hold quote from the service; if that
read fails, the plan is still printed, without the quote.

## `cli service status`

Whether the service is reachable (`status`), and the models you can use:
`models` and `default_model`. It sends no API key, so it works before login.
Any other field the service returns is ignored.

```text
Service: ok
Models: model-sol, model-luna (default model-sol)
Next: prose cli service triage
```

## `cli service triage`

Where the session stands, in one call: start a session with it. It reads
health without a key, then, with a usable key, the balance, the default
organization, the five newest runs and the jobs. The result has a section per
read (`health`, `wallet`, `organization`, `runs`, `jobs`), a `credential`
section and `nextCommands`:

- **A problem per section.** Each section is its fields with `problem: null`,
  or `{problem}` holding the error the matching single command would have
  ended with (`wallet balance`, `org show default`, `run list`, `job list`).
  One failing read never hides the others, and the command exits 0 whenever
  it produces the report. Only cancellation (24) and invocation errors (2)
  end it early.
- **Credential diagnosis.** `credential` is `{variable, source, state,
  problem}`. `variable` is always `OPENPROSE_API_KEY`, and `source` is
  `environment`, `store` or `none`. `state` is one of:
  - `valid`: a read that needs the key succeeded.
  - `unverified`: a key is present, but no read that needs it succeeded.
  - `missing`, `malformed`, `rejected` or `unavailable` (the OS store
    failed). `problem` says why.

  With no usable key the account sections are `null`, no keyed request is
  sent, and the first next step is the variable to set. A rejected key stops
  the remaining keyed reads.
- **Unreachable service.** When `/health` gets no response at all, the
  account sections are `null`, nothing else is sent, and the next step
  retries `cli service status`.
- **Prices only.** The balance keeps `*_cents` and `*_dollars` (never
  `*_nanos` or the customer id). Runs are `cli run list` summaries (price
  fields only). Jobs keep `id`, `type`, `name`, `nextFireAt`, `lastEventAt`,
  `lastRunId` and `lastError`, with `total` and `max`. `health` keeps
  `status`, `default_model` and `models`.
- **`nextCommands` from state.** Each entry is `{why, argv, env}`, with
  `argv` the words after `prose`, keeping the output mode:
  1. the credential variable when the credential has a problem;
  2. `service status` when the service did not answer;
  3. `run watch RUN_ID` for up to three live runs (`queued`, `pending`,
     `starting`, `running`, `input_needed`, `awaiting_input`);
  4. `wallet topup --amount-cents 500 --preview` when the available balance
     is below $5.00;
  5. `job show ID` for up to two jobs that report `lastError`.
- **Human output.** At most 25 lines: one line per section, up to five runs
  and five jobs, then `Next:` with one copyable command and its `# why` per
  line.

```text
Service: ok, default model model-sol
Credential: valid, OPENPROSE_API_KEY from store
Wallet: $3.12 available, $6.12 reserved (low: below $5.00)
Organization: exowner1 (admin)
Runs: 1 recent, 1 live
  run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  running  model-sol  2026-09-24T08:35:02.570Z
Jobs: 0 of 5 allowed
Next:
  prose cli run watch run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  # run run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa is running
  prose cli wallet topup --amount-cents 500 --preview  # the available balance $3.12 is below $5.00
```

## `cli model list`

Reads the authenticated `/models`, which can include models
that the anonymous `/health` list omits; there is deliberately no fallback to
`/health`. The result keeps the service order of `models` and the
`default_model`. Human mode marks the default and ends with `Next: prose cli
run quote`; `--output jsonl` prints one `openprose.service-record/1` line per
model and a page line whose `meta` carries `default_model`.

```text
model-sol (default)
model-luna
Next: prose cli run quote
```

A newer service also sends `catalog`: every model it names with a `status`.
The CLI projects each entry through an allowlist (`id`, `status`, `tier`,
`summary`, `successor`) into `result.catalog`, skips entries without a valid
model id or status and never shows a `deprecated` model; without a catalog
(older servers) the output above is unchanged. No model id is built into the
CLI: every line comes from the response. With a catalog, human mode prints
sections: the models this account can run (`models`, default marked), the
premium models (`paid_top_up`) with their summary and a top-up preview, and
the older ids the service still accepts (`hidden`) with the model to use
instead.

```text
Available:
  model-sol (default)
  model-luna
Premium — unlocks with any wallet top-up:
  model-astra: Most capable model for hard, long tasks. Higher rate per run. Unlocks with any wallet top-up.
Next: prose cli wallet topup --amount-cents 500 --preview
Also accepted (not recommended):
  model-sol-legacy → use model-sol
Next: prose cli run quote
```

A premium model is refused until the wallet has had any top-up: the
service's 402 `paid_top_up_required` (403 `paid_model_required` from older
servers) on `run submit`, `run quote`, `job create|update` or `program draft`
is `SERVICE_PREMIUM_MODEL_LOCKED` (exit 10; the headline is about the
model, not the balance) with `details.reason` built from the
service's `summary`, `details.model`, `details.tier`, and an Action and
`details.suggestedArgv` that preview a top-up (`cli wallet topup
--amount-cents 500 --preview`). `run submit` and `program draft` refuse a
`--model` the catalog marks premium the same way before any plan, and accept
a `hidden` one.

## `cli example list` and `cli example show`

`example list` reports each private example's `id` and `label`. Only
sources on the service origin under the private example route are followed
(relative, or absolute on exactly the service origin); an example published
anywhere else, such as on another host, is marked `viewOnWeb: true`, with its
`webUrl` when the service names an https web page for it, and `example show`
refuses it with `SERVICE_PROTOCOL_INVALID`. The service route is never
printed, and the CLI never contacts another origin.

`example show <NAME>` resolves the name through the listing: an exact id, else
an id or a label compared case-insensitively (`"Slack hello"` maps to
`slack-hello`; human mode notes the mapping on stderr). An unknown name is
`SERVICE_RESOURCE_NOT_FOUND` naming `cli example list`; when one listed id is
within the manifest's edit distance it is named too (`did you mean
"slack-hello"?`) and `details.suggestedArgv` is that `example show` command
. A name with a control character exits 2 before any request.
It then fetches the source
and returns `bytes`, `sha256` and the text as `content`. With
`--output-file F` the bytes go to a new file instead (`written: true`, no
`content`); `F` must name a file (not empty, no trailing `/`, final segment
not `.` or `..`), must not exist, and its directory must. All of this is
checked before any request, so a failed command never contacts the service or
leaves a file behind. The path is resolved by the operating system, never
normalized lexically, so `missing/../x.md` fails the same way in both ports. The bytes must be
UTF-8 text without control characters other than tab, LF and CR. Human mode
prints the text verbatim.

## `cli repo list`

Flattens the GitHub App installations the account can reach into
`repositories` (`id`, `name`, `full_name`, `private`, `default_branch` when
known), sorted by `full_name` then `id`, one entry per repository. A 401 means
the account's GitHub link is missing or stale (`GITHUB_LINK_REQUIRED`, exit
10); the API key itself is valid. When repositories are not available for
the account, the command reports `SERVICE_FEATURE_DISABLED`.

## Errors

Every malformed success is `SERVICE_PROTOCOL_INVALID` with `details.reason`
naming the response and the first field that failed
(`unexpected GET /health response: models`); values are never echoed. Other
failures follow the shared service error classification (body code, then route,
then status).
