# OpenProse service guide

## Start here

`prose cli` commands reach the hosted OpenProse service. They never
prompt and never run anything locally. This guide is the workflow view;
the per-command facts come from these commands:

- `prose cli service triage --json`: where this session stands, in one call:
  health, whether the key works (or the variable to set), balance, default
  organization, recent runs, jobs and `nextCommands`. Each section has its
  own `problem`; it exits 0 whenever it produces the report.
- `prose cli service capabilities --json`: grammar, exit code of every error
  code, environment variables and every command with examples, on one line.
- `prose cli service operations --json`: every command with its arguments,
  options and effect, as JSON.
- `prose cli <COMMAND> --help`: one command, ending with its exit codes.
- `prose cli service guide --json`: this guide as
  `{"sections":[{"id","title","body"}]}` in the usual envelope.

Program files (`.prose.md`) are not described here: read a working one with
`prose cli example list` and `prose cli example show NAME`. New to the
service? Start with "Your first program" below, then "Run it every day",
"Scripting", "What will it cost", "Headless and CI keys" and "Sharing".

## Your first program

Start from a working example, run it once, then save it.

```sh
prose cli example list
prose cli example show NAME --output-file hello.prose.md
prose cli run submit hello.prose.md --preview
prose cli run submit hello.prose.md --yes
prose cli program save hello hello.prose.md
```

1. `cli example list` prints the example names. `cli example show` with
   `--output-file` writes one to a new file; edit it with any editor.
2. `--preview` shows what the run would do and what it would reserve, and
   sends nothing. `--yes` starts the paid run and streams its answer.
3. `cli program save SLUG FILE` keeps it as your own private program. Run
   the saved program with `prose cli run submit --from hello --yes`.

Tools a run can use depend on its environment (`--environment`): `builtin`
has file tools only; `linux` adds a shell, without network access. A
program reaches the web through the `browser:interact` tool.

## Run it every day

A job runs a saved program on its own. A schedule job repeats every
`interval_seconds` (60 to 2678400; 86400 is once a day). The first run
starts about one second after the job is created. There is no cron
expression or time of day yet.

```sh
prose cli program save hello hello.prose.md --json
prose cli program show hello --json
printf '%s' '{"type":"schedule","program_ref":"OWNER/hello@REV","interval_seconds":86400}' > daily.json
prose cli job create --spec-file daily.json --preview
prose cli job create --spec-file daily.json --yes --json
prose cli job list
prose cli job delete JOB_ID --yes
```

`program_ref` is the pinned reference `result.program.ref` that
`program save` and `program show` print (`OWNER/SLUG@REV`). Each run is paid
like any other. `cli job list` shows the next run time; `cli job delete`
stops the schedule for good.

To run at a fixed time of day instead, let the operating system's scheduler
(cron, launchd) run one command with `OPENPROSE_API_KEY` set:

```sh
prose cli run submit --from OWNER/hello --yes --json | jq .result.run.response
```

## Scripting

- Pass `--json` and read `result`; on an error, `problem.code` and
  `problem.details`. Every exit code is in the table in "Exit codes, resume
  and detach"; a script branches on it with `case $?`.
- The run id is `result.runId` (`result.run.run_id` once the run ends).
- For a long run, submit with `--detach` (exit 0 as soon as the run id is
  known), then follow it with `prose cli run watch RUN_ID --wait 10m --json`.
  If `--wait` passes first, the exit is 21 and `problem.details.resumeArgv`
  is the command to run next. Never submit again to resume.
- `cli run list` and `cli wallet events` return one page. Pass
  `result.nextBefore` to `--before` for the next one; it is `null` on the
  last page.

```sh
prose cli run submit --from OWNER/hello --yes --detach --json > run.json
prose cli run watch RUN_ID --wait 10m --json
prose cli run list --limit 50 --before CURSOR --json
```

## What will it cost

A run reserves a hold before it starts: money set aside from the wallet
(currently $1.02), not the price. The price is known only after the run
settles: `price_cents` in `prose cli run show RUN_ID --json`, of which
`environment_price_cents` is the environment's share. What the run did not
use is released. Short runs usually cost a few cents.

Estimate from your own history: `prose cli wallet usage` prints runs and
prices by day, and `prose cli wallet balance` shows what is available and
what is reserved right now. `prose cli run quote --json` reports the hold.

Premium models unlock with any wallet top-up; `prose cli model list` shows
each model's status.

## Headless and CI keys

A machine without a person at a browser uses an API key in
`OPENPROSE_API_KEY`; it wins over the stored key. Keep it in the CI system's
secret store and set it only for the commands that need it.
`prose cli auth login` stores its key in the operating system credential
store, which a launchd or cron job cannot reach without a login session:
set `OPENPROSE_API_KEY` in the job instead. Check a key with
`prose cli auth status --json`.

## Sharing

`prose cli run share RUN_ID --yes` creates a public link to a run's outputs.
Anyone with the link can open it for 24 hours, and it cannot be revoked. To
give specific people access, invite them to your organization instead:
`prose cli org invite --help`.

## Grammar

```text
prose [--output human|json|jsonl] cli <NOUN> <VERB> [ARGUMENTS] [OPTIONS]
```

- Every service command starts with `cli`. Without it (`run submit` or
  `wallet balance` right after `prose`) the command exits 2 and
  `details.suggestedArgv` names the `cli` command.
- Global options go before `cli`; command options go after the command path.
  `--output` may also follow the command path.
  A command option before `cli` (`--json`, `--yes`, `--limit 5`) exits 2 and
  `details.suggestedArgv` moves it after the command path.
- `--json` is `--output json`: one JSON object on one line. Streaming
  commands (`cli run submit`, `cli run watch`) print one event per line with
  `--output jsonl` and end with exactly one `service.completed`,
  `service.failed` or `service.detached` line (`service.detached` is exit 21:
  the run continues). List commands (`cli run list`, `cli job list`,
  `cli model list`, ...) print one `openprose.service-record/1` line per item
  with `--output jsonl`, then one `openprose.service-page/1` line with
  `count`, `nextBefore` and the rest of the result in `meta`. That last line
  carries `exitCode`, the process exit code. Every JSON line has its keys
  sorted, so the same response prints the same bytes.
- Times the service sends as epoch milliseconds (`createdAt`, `nextFireAt`,
  `updated_at`, ...) also appear as `<name>_iso` (RFC 3339 UTC). Human output
  shows money in dollars and ends with `Next:` commands you can copy.
- Nothing is guessed. A misspelled or foreign word exits 2
  (`INVOCATION_INVALID`); `details.suggestedArgv` is the corrected argv (the
  words after `prose`). Rerun it; it keeps your output mode. A few exact
  spellings are aliases and run their command: `cli run status` and
  `cli run get` are `cli run show`, `cli run logs`, `cli run tail` and
  `cli run wait` are `cli run watch`, `cli run create` and `cli run start` are
  `cli run submit`, `cli program push` and `cli program upload` are
  `cli program save`, and `cli program rm` and `cli job rm` are `delete` (still
  with `--yes`). The manifest lists them in `grammar.intentInference.commandAliases`.
  A suggestion is a complete command that parses: a group gains its verb
  (`cli model list`), a dollar amount becomes cents (`--amount-cents 500`),
  and an out-of-range `--limit` becomes the nearest accepted value. When the
  fix reads standard input, `details.suggestedStdin` holds what to pipe in;
  a secret (a credit code, an invitation token) is never echoed, so pipe
  your own.

## Credentials

The key, first match wins:

1. `OPENPROSE_API_KEY`, when set and non-empty;
2. the key `prose cli auth login` stored in the operating-system credential
   store (login needs a person at a browser).

A variable always wins over the stored key, so a bad variable must be fixed
or unset. `prose cli auth status --json` reports `credentialSource`.
`SERVICE_AUTH_REQUIRED` names `details.credentialVariable` and
`details.credentialProblem` (`missing`, `malformed` or `rejected`), and its
Action follows where the key came from: an environment key is replaced or
unset, never fixed by `cli auth login`. Nothing ever falls back to another
key. The output mode is `--json` or `--output`, then `PROSE_OUTPUT`, then
human.

## Confirm and preview

A command that spends money, publishes, deletes or cannot be undone needs
`--yes`. Without it nothing is changed: it exits 2 with `CONFIRMATION_REQUIRED`,
`details.reason` (what `--yes` would do, for example that rotating a secret
invalidates the old one now), `details.plannedRequest` (the method, what the
request does as `description`, its non-secret inputs as `summary`: model, programRef, inputKeys,
amount_cents, slug; and for `cli run submit`, `cli program draft` and a paid
`cli job create` the service's flat hold quote), `details.confirmArgv` and
`details.previewArgv`. `--preview` prints the same plan, exits 0 and changes
nothing. An unknown `--model`, an `--environment` or `--runtime` that
`prose cli service status --json` does not list (`environments`), an input the
program's `Parameters` section does not declare (or a declared one that is
missing and not marked optional), an invalid organization slug or a job spec
the service would reject fails here, before any confirmation, with every
problem listed:

```sh
prose cli run submit hello.prose.md --preview
prose cli run submit hello.prose.md --yes
```

`prose cli service operations` shows which commands need `--yes` (CONFIRM).

## Exit codes, resume and detach

| Exit | Meaning | What a script does |
| --- | --- | --- |
| 0 | success | read `result` |
| 2 | `INVOCATION_INVALID` or `CONFIRMATION_REQUIRED`; nothing was sent | rerun `details.suggestedArgv`, or add `--yes` on purpose |
| 10 | service or credential error | read `problem.code` and `details.reason`; retry only when `problem.retryable` is true |
| 20 | this installation cannot run the command | reinstall the CLI; retrying will not help |
| 21 | detached: the run continues | run `details.resumeArgv`; never submit again |
| 22 | the run failed, or the submission is ambiguous | read `details.reason`; if ambiguous, rerun `details.resumeArgv` (it reuses the same session, never a new run) |
| 24 | cancelled | stop; nothing to resume |

In JSON mode the error is `problem` (`code`, `retryable`, `action`,
`details`), with `result` null. This holds for every `cli` command line,
including a near miss or a misplaced flag: then `operation` is `cli` if the
argv names no operation. Only the language and the commands for this machine
(listed by the top-level help) print a bare runner error. The exit of every
code is in
`prose cli service capabilities --json` (`exitCodes`).

Exit 21 means the CLI stopped following a run that keeps running:
`--wait` passed (`SERVICE_WATCH_DEADLINE`) or Ctrl-C or a lost stream
(`HOSTED_RUN_DETACHED`); both are not retryable and carry
`details.resumable: true`. A `--wait` that passes before the run id arrives
keeps reading until the id is known, so it is still exit 21 with the run
named. A successful `--detach` exits 0 with `result.detached`,
`result.resumeArgv`, `result.cancelArgv` and `result.run` as
`{run_id, status}`. Results carry the run id as `result.run_id` and
`result.runId`.
Resume with `resumeArgv` (a `cli run watch` command with `--session`). Never resume by
submitting again: a new submit is a second paid run. Only
`prose cli run cancel RUN_ID --yes` cancels; on a run that already ended it
exits 0 with `result.status` `already_ended`. Steering an ended run with
`cli run input` is not retryable: read it with `details.suggestedArgv`.

`SERVICE_RESOURCE_NOT_FOUND` (10) is not retryable. `details.resource`
names the kind and id, and `details.suggestedArgv` lists the ones that
exist: run it and retry with an id it prints. A well-formed run id the
service does not know (`cli run show`, `cli run watch`) and a `--file` the
run does not list are this error. A run id with fewer than 64 hex digits was
cut off when copied: it exits 2 before any request. `latest` in place of a
run id is your newest run (`cli run show latest`). `program delete` and
`job delete` with `--yes` of something already gone exit 0 with
`result.alreadyAbsent`.

`RUN_SUBMISSION_AMBIGUOUS` (22) means the connection was lost before the
run id was known, and the CLI did not repeat the submission. Rerun
`details.resumeArgv`: the same `cli run submit` with the same `--session`
and `--detach`. A session never starts a second run: `run submit --session S`
of a session that already has a run (in this machine's journal, or as the
service reports) returns that run with `result.reused: true` and exit 0.
`prose cli run list --limit 5 --json` (`details.suggestedArgv`) lists recent
runs.

A run its owner stopped has `cancelled: true` in `cli run show` and
`cli run list`, and `cli run watch` of it exits 24.

## Paging

`cli run list` and `cli wallet events` are paged. Pass
`result.nextBefore` to `--before` for the next page; it is `null` on the last
page.
The cursor is opaque. Human mode prints a `Next page:` line with the full
command, and `--output jsonl` carries `nextBefore` in the page line.

```sh
prose cli run list --limit 50 --json
prose cli run list --limit 50 --before CURSOR --json
```

`cli result list` takes `--limit` only (at most 100) and has no cursor.

## Get a run's answer

- While it runs: human mode prints the program's text on stdout. With
  `--json`, the answer is `result.run.response`; `result.run.files` lists the
  files it wrote.
- After a detach (runs submitted from this machine): replay the whole stream
  with `prose cli run watch RUN_ID --json` and read `result.run.response`.
  For a run started elsewhere, the same command reports an ended run's
  status and files from its record (`result.source` is `record`, no
  response text).
- Any run, from anywhere: `prose cli run show RUN_ID --json` gives status,
  files and price but never the response text. `result.run.response` is the
  run's text as written, trailing whitespace included. Read a file with
  `prose cli run show RUN_ID --file outputs/result.json`, or fetch them all
  into `./RUN_ID` with `prose cli run download RUN_ID` (or `--output-dir DIR`).
- A result someone published (`OWNER/SLUG`) is not a run record:
  `prose cli result show OWNER/SLUG --latest --json`.

## Recipes

### Submit and wait

```sh
prose cli run quote --json
prose cli run submit hello.prose.md --input topic=otters --preview
prose cli run submit hello.prose.md --input topic=otters --yes --json > run.json
jq -r '.result.run.response' run.json
```

`cli run quote` is the wallet hold a run reserves, not its price; the settled
price is `result.run.price_cents`. A saved program runs with
`--from SLUG` (your own) or `--from OWNER/SLUG@REV`; every program record
(`program list|show|save|revisions --json`) carries that pinned reference as
`ref`, and `prose cli program show SLUG@1 --json` resolves a revision number
of your own program to it. On exit 21, rerun
`problem.details.resumeArgv`; for a long run submit with `--detach` and follow
with `prose cli run watch RUN_ID --wait 10m --json`.

### Publish a result

A published result is a completed run of a saved revision of a public
program that wrote `outputs/result.json`.

```sh
prose cli program save haiku haiku.prose.md --json
prose cli run submit --from haiku --yes --json
prose cli program visibility haiku public --preview
prose cli result publish haiku --run RUN_ID --yes --json
prose cli result show haiku --json
```

Making a program public exposes its source; the CLI never adds `--yes` to that
step for you. Reading published results needs no key; a bare `SLUG` (your
own program) is resolved with your key first, and without a publication id
`result show` reads the newest.

### Create a webhook job

A job starts runs on its own. The spec is snake_case JSON; a test-mode webhook
receives events and never starts runs.

```sh
printf '%s' '{"type":"webhook","name":"my-hook","delivery_mode":"test"}' > hook.json
prose cli job create --spec-file hook.json --preview
prose cli job create --spec-file hook.json --yes --json
prose cli job deliveries JOB_ID --json
```

`result.endpoint` and `result.signing_secret` appear only in the create result
(and in `cli job rotate-secret`); store them then. `result.endpointUrl` is the
absolute URL to configure in the sender; `cli job show` repeats it. `prose cli job create --help`
lists the schedule and webhook spec keys; `prose cli job list --json` lists the
job types. The keys each type accepts are the `spec` of `job.create` in
`prose cli service operations --json`; a spec is checked against it before any
request and every problem is reported at once in `details.violations`. A
webhook with no `program_ref` starts no runs, so its plan has effect `write`
and no hold.
