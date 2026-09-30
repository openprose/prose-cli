# Hosted runs: quote, submit, watch, input, cancel

`prose cli run …` runs an OpenProse program on the hosted OpenProse service. Every run is a **live session**: it can be streamed, replayed from any sequence number, steered and cancelled. Nothing ever prompts; commands that spend money or change a run need `--yes` (or `--preview` to see the exact request without sending it). Keys work as described in [credentials](credentials.md).

| Command | What it does | `--yes` |
| --- | --- | --- |
| `cli run quote [--environment ENV]` | The wallet hold a run reserves while live (price-free: holds only). `ENV` must be advertised by `/health`. | no |
| `cli run submit FILE\|- \| --from OWNER/SLUG[@REV] [options]` | Submit and stream until the run finishes, `--detach`, the `--wait` deadline, or an interrupt. | **yes** |
| `cli run watch RUN_ID [--after N] [--wait DUR] [--session UUID]` | Replay events after sequence `N`, then follow live. Without a live session on this machine, report an ended run's outcome from its record. | no |
| `cli run input RUN_ID TEXT [--id UUID] [--session UUID]` | Queue an instruction for a live run. A run that already ended is refused (`SERVICE_WRITE_CONFLICT`, not retryable), from any machine. | **yes** |
| `cli run cancel RUN_ID [--session UUID]` | Cancel a live run, then report the wallet's available and reserved balance. A run that already ended is `already_ended` (exit 0), also when it was started on another machine. | **yes** |

Records of finished runs (`run list|show|download|share`) are in [run records](run-records.md).

## Quote first

```sh
prose cli run quote --json
```

```json
{"schema":"openprose.service-operation/1","operation":"run.quote","interaction":"run.quote",
 "result":{"environment":"builtin","hold":{"hold_usd":"1.02","hold_cents":102,"ttl_seconds":900},"holdBasis":"flat hold, independent of program and model; a run's price is known only after it settles","note":"Unused hold is released when the run settles."},"problem":null}
```

The quote is the hold, not the price: a flat hold, independent of program and model (`result.holdBasis` says so, and human output adds a `Price:` line naming `run show`); the settled price appears in the finished run (`price_cents`). An unknown `--environment` is rejected before `/run/quote` is called (`INVOCATION_INVALID`, naming the advertised environments).

## Submit

```sh
prose cli run submit hello.prose.md --model model-luna --yes --output jsonl
```

- **Without `--yes`** nothing is sent: exit 2 `CONFIRMATION_REQUIRED` with `details.plannedRequest` (method, `description`, body SHA-256 and size, effect `money`; never the service route) plus `plannedRequest.quote` (the current flat hold), `plannedRequest.summary` (the non-secret body fields: `model`, `reasoning_effort`, `programRef` for `--from`, and `inputKeys`, the sorted input names without values), `details.reason` (what `--yes` does: reserve the hold and start a paid run), and `details.confirmArgv` / `details.previewArgv` — the same command with `--yes` or `--preview`. `--preview` prints the same plan and exits 0.
- **Source:** a local file (at most 1 MiB of UTF-8, not blank), `-` for standard input, or `--from OWNER/SLUG[@REV]` (without `@REV` the latest revision is resolved and a pinned `program_ref` is sent). URLs are refused.
- **Inputs:** `--input KEY=VALUE`, `--input KEY=@FILE` (UTF-8, at most 1 MiB) and `--inputs-file FILE` (a JSON object of strings). `--input` overrides a key from `--inputs-file`; the same `--input` key twice is an error. The whole submission is at most 8 MiB.
- **Model and placement:** `--model`, `--reasoning-effort`, `--environment`, `--runtime` (see `cli model list`). A `--model` is checked against `GET /models` before the confirmation gate: a name that is not offered and not the default is `INVOCATION_INVALID` listing the three nearest offered models, with `details.suggestedArgv` retrying with the nearest; if the lookup itself fails the service decides.
- **Repositories:** `--repo OWNER/NAME[@BRANCH]` (repeatable, read-only context) and `--commit-output OWNER/NAME[@BRANCH]` (must also be a `--repo`).
- **`--detach`** returns as soon as the run id is known (`result.detached: true`, `result.run: null`). **`--wait DUR`** (`90s`, `10m`, `2h`; default 30m, at most 6h) detaches with exit 21 when it passes.

Before contacting the service the CLI writes the session UUID to the run journal, then sends `POST /run?live=1&session=<uuid>` with `X-Session-Id` and `Accept: text/event-stream`. It never sends a non-live run and never repeats a submission blindly: if the connection drops before the first event it resubmits **once with the same session**; the service's `409` for that session proves the first attempt was admitted and charges nothing more. The run id is then recovered from `X-Run-Id` when the service sends it; otherwise the command exits `RUN_SUBMISSION_AMBIGUOUS` (22) with `details.session`, and the journal keeps the entry so you can find the run with `cli run list`.

### Output

`--output jsonl` prints one `openprose.service-event/1` line per event and ends with exactly one terminal line whose `data` is the JSON-mode envelope: `service.completed` (exit 0), `service.detached` (exit 21: the run continues, resume with `details.resumeArgv`) or `service.failed` (every other failure):

```json
{"schema":"openprose.service-event/1","runId":"run_…","sequence":1,"at":"…","type":"status","data":{"status":"running","controls":{"steer":true,"stop":true}}}
{"schema":"openprose.service-event/1","runId":"run_…","sequence":3,"at":"…","type":"text_chunk","data":{"text":"ok"}}
{"schema":"openprose.service-event/1","runId":"run_…","sequence":null,"at":null,"type":"service.completed","data":{"…":"envelope","result":{"runId":"run_…","session":"…","detached":false,"afterSequence":4,"run":{"run_id":"run_…","status":"completed","cancelled":false,"files":["outputs/result.json"],"price_cents":2,"…":"…"}}}}
```

- Event types: `status`, `agent_activity` (allowlisted fields only), `text_chunk`, `browser_live_view_changed`, `history_truncated` (the service's replay-gap marker) and `error`. Any other service event becomes `unrecognized` with only its sanitized name and never stops the stream. Heartbeats are ignored.
- `--output json` prints only the final envelope. Human mode writes the program's text to stdout and status, activity and the summary to stderr.
- `result.run` is a closed projection of the service's `run_complete`: `files` is always an array of paths, and signed `file_urls` are never printed.

## Exit codes

| Exit | Code | When | Details |
| --- | --- | --- | --- |
| 0 | — | `run_complete` with status `completed`, a detached result, `run watch` of a completed run read from its record (`source: "record"`), or `run cancel` of an ended run (`status: "already_ended"`) | |
| 2 | `CONFIRMATION_REQUIRED`, `INVOCATION_INVALID` | missing `--yes`; bad arguments, files over the limits; `run input`, `run cancel` or `run watch` without a journal session of a run that has not ended (live elsewhere, or unknown) | `plannedRequest`, `confirmArgv`, `previewArgv`; `reason`, `suggestedArgv` (the corrected or follow-up command the Action names; `cli run show RUN_ID` for a run live elsewhere) |
| 10 | `SERVICE_BALANCE_INSUFFICIENT`, `SERVICE_PREMIUM_MODEL_LOCKED`, `SERVICE_FEATURE_DISABLED`, … | the service refused the submission (body code first) | `serviceStatus`, `serviceCode` |
| 10 | `SERVICE_PROTOCOL_INVALID` | `run watch` from sequence 0 was closed cleanly without `run_complete` | `runId`, `afterSequence`, `reason` naming `cli run show` |
| 10 | `SERVICE_WRITE_CONFLICT` (not retryable) | `run input` on a run that has ended or is being cancelled | `runId`, `reason`, `serviceMessage` (from the service's 409) or `source: "record"` (read without a session), `suggestedArgv` (`cli run show RUN_ID`) |
| 21 | `SERVICE_WATCH_DEADLINE` (not retryable) | `--wait` passed; the run continues. JSONL terminal line `service.detached` | `runId`, `afterSequence`, `resumable: true`, `resumeArgv`, `cancelArgv` |
| 21 | `HOSTED_RUN_DETACHED` (**not** retryable) | interrupt (Ctrl-C), or the event stream was lost before `run_complete`: **the run continues and is not cancelled**. JSONL terminal line `service.detached` | `runId`, `afterSequence`, `session`, `resumable: true`, `resumeArgv` (`cli run watch … --after N --session S`), `cancelArgv` |
| 22 | `HOSTED_RUN_FAILED` | the run finished with another status (from the stream, or from the run record when `run watch` has no journal session), or the stream closed after an `error` event without `run_complete` | `runId`, `afterSequence` and `session` from a stream; `files` and `source: "record"` from a record; `reason` |
| 22 | `RUN_SUBMISSION_AMBIGUOUS` | the submission could not be confirmed and was not repeated: two drops, an interrupt or deadline before the run id was known, a duplicate-session 409 that does not name the run, or any 409 after a resubmission | `session`, `serviceStatus` for a 409 |
| 24 | `HOSTED_RUN_CANCELLED` (not retryable) | the service reported `run_complete.cancelled` (or a record status `cancelled`); terminal | `runId`, `afterSequence` (stream) or `files`, `source` (record) |

`RUN_SUBMISSION_AMBIGUOUS` comes only from `run submit`; `run watch` never submits. Each command's `--help` ends with its own exit codes, which are rendered from the manifest's `exitCodes` and checked against these cases.

Ctrl-C, a lost stream and the deadline only detach. `resumeArgv` and `cancelArgv` repeat, in JSON or JSONL mode, the invocation's `--output`. Resume with `details.resumeArgv`, never by running `run submit` again: a new submit is a new session and a second paid run. Both exit-21 codes are marked not retryable for that reason, with `details.resumable: true`. A successful `--detach` (exit 0) carries the same `result.resumeArgv` and `result.cancelArgv`, and the human `Follow it with:` line is exactly `resumeArgv`, `--session` included. `run submit --session S` of a session this machine's journal already maps to a run sends nothing and returns that run with `result.reused: true`. `--from` takes `[OWNER/]SLUG[@REV]` as `program show` reads it: a bare SLUG is your program, `@N` (or `@revN`) is its revision N, and a pinned `@REV` of your own program must be one of its revisions (a commit id is named as one, with the rev_id to pass); the preview shows the pinned reference. `--reasoning-effort` is one of `minimal`, `low`, `medium`, `high`, `xhigh`, checked before anything is sent. Only `cli run cancel --yes` cancels a run.

Only the duplicate-session 409 (it names a run through `X-Run-Id` or `run_id`, or it carries the service's `This session already has a run.` text and no `code`) is treated as proof of admission. Any other 409 on `POST /run`, such as `model_policy_mismatch` or `Execution admission was refused.`, is classified by the manifest like any other response (for example `SERVICE_REQUEST_REJECTED` or `SERVICE_WRITE_CONFLICT`). After a resubmission, any 409 that does not name the run is `RUN_SUBMISSION_AMBIGUOUS`.

## Watch, steer, cancel

`run watch`, `run input` and `run cancel` need the run's live session. The CLI reads it from the local run journal (runs submitted from this machine); `--session UUID` overrides it. Runs started elsewhere (web app, jobs, another machine) can be read with `cli run show`; a live one cannot be steered or cancelled from here without its session. With neither `--session` nor a journal entry, all three read the run record first (`GET /runs/{id}`; for `input` and `cancel` before the `--yes` check):

- `run cancel` of an ended run exits 0 with `{"runId","status":"already_ended","runStatus"}`: nothing is sent and the wallet is not read, so `--yes` is not needed.
- `run input` of an ended run is `SERVICE_WRITE_CONFLICT` (not retryable) with `details.source: "record"` and `suggestedArgv` `cli run show RUN_ID`; nothing is sent.
- A run that has not ended is `INVOCATION_INVALID` (2) whose Action says to cancel it (or send the instruction) from the machine that submitted it, or to pass its session with `--session UUID`.

`run watch` of such a run:

- an ended run (record status `completed`, `error`, `failed`, `timeout` or `cancelled`) is reported like a replay: exit 0 with `result.session: null`, `result.afterSequence: 0`, `result.source: "record"` and `result.run` projected from the record (status, files, price, usage, error; no `response` text, which only the stream carries), or exit 22/24 with `details.files`. Human mode prints the outcome, the file list and a `cli run show RUN_ID --file PATH` command.
- a run with no ended record (404: records are written when a run ends) or another status is `INVOCATION_INVALID` (2): the reason says the live session is in the run journal of the machine that submitted it (watch there, or pass `--session UUID`), and `suggestedArgv` is `cli run show RUN_ID` for when it ends. `run input` and `run cancel` report a live run the same way.

```sh
prose cli run watch run_… --after 3 --output jsonl   # events 4, 5, … then the terminal line
prose cli run input run_… "Also write a summary." --yes --json
prose cli run cancel run_… --yes --json
```

- `watch --after N` never prints a sequence at or below `N`. A finished run replays everything and ends with its terminal line. The service keeps a live run's stream open and closes it cleanly only once the run is terminal, so when `--after` is at or past the terminal event the CLI replays once from 0 (printing nothing already seen) and reports the run's real outcome: exit 0, `HOSTED_RUN_FAILED` or `HOSTED_RUN_CANCELLED`.
- `input` sends `{id, text}` (1–4000 characters). The id is minted unless `--id UUID` is given; reuse the same `--id` to retry idempotently.
- `cancel` returns `{"runId","status":"cancelling","balance":{available_*, posted_*, reserved_*}}`. A cancelled run's hold can stay **reserved** for up to the quote's `ttl_seconds` (900 s at the time of writing); the CLI reports it and never claims it was released. Watch the run to see its `run_complete` (exit 24, `HOSTED_RUN_CANCELLED`).
- Cancelling a run that has already ended (the service's 409 `This run has already ended.`, or, without a session on this machine, its ended record) is not an error: exit 0 with `{"runId","status":"already_ended","runStatus"}`, where `runStatus` is the run record's status (null while the record is not written yet). The wallet is not read. Cancel is therefore safe to repeat, from any machine.
- Steering a run that has ended (409 `This run is no longer accepting instructions.`, also sent while a cancel is stopping it, or an ended record read without a session) is `SERVICE_WRITE_CONFLICT`, **not retryable**, with `details.reason`, an Action saying not to retry, and `details.suggestedArgv` `cli run show RUN_ID`. Other 409s on `run input` (an `--id` reused with different text) keep the generic conflict Action.

## Known service behavior (2026-09-24)

- `GET /run/quote?environment=<unknown>` crashes with a 500; the CLI validates against `/health` first.
- A cancelled run's manifest status is `error` with no cancelled flag; only the live stream's `run_complete.cancelled` distinguishes a cancel, so `cli run show` reports the status verbatim.
- The service does not yet send `X-Run-Id`, so a dropped-then-409 submission is reported as `RUN_SUBMISSION_AMBIGUOUS` until it does.
