# Published results

A **published result** is one completed run that the owner of a public program
has made public. Anyone can list and read a program's published results; only
the owner can publish or unpublish. The commands are service
operations and behave the same in the Rust and Bun builds.

| Command | Request | Key | `--yes` |
| --- | --- | --- | --- |
| `prose cli result list [OWNER/]SLUG [--limit N]` | `GET /p/{owner}/{slug}/results?limit=N` | none | no |
| `prose cli result show [OWNER/]SLUG PUBLICATION_ID` | `GET /p/{owner}/{slug}/results/{id}` | none | no |
| `prose cli result show [OWNER/]SLUG [latest\|--latest]` | `GET /p/{owner}/{slug}/results/latest` | none | no |
| `… --raw [--output-file FILE]` | then `GET /p/{owner}/{slug}/results/{id}/raw` | none | no |
| `prose cli result publish SLUG --run RUN_ID` | `POST /programs/{slug}/results` `{"run_id"}` | yes | yes |
| `prose cli result unpublish SLUG PUBLICATION_ID` | `DELETE /programs/{slug}/results/{id}` | yes | yes |

## Reading results (no key needed)

`result list` and `result show` never send an `Authorization` header, so they
work without an API key and never reveal who is asking.

```sh
prose cli result list someone/haiku --json
prose cli result show someone/haiku --latest --json
prose cli result show someone/haiku --latest --raw --output-file result.json
```

- `OWNER/SLUG` names the program, never a revision. `OWNER/SLUG@REV` is refused
  with the corrected argument, because results belong to the program.
- `--limit` is 1 to 100 (default 20). The service currently returns at most 50,
  newest first. There is no cursor.
- A bare `SLUG` is your own program: its owner is read from
  `GET /programs/{slug}/revisions`, which needs your key; then the public read
  is anonymous as usual.
- `result show` without a `PUBLICATION_ID` reads the newest publication, the
  same as `--latest` or the word `latest` in place of the id. An
  id together with `--latest` exits 2.
- The result is `{publication, manifest}`. The manifest keeps the server's field
  names (`price_cents`, `usage`, `files`, …). The service's `raw_url` and signed
  `file_urls` are dropped.
- `--raw` fetches the exact published bytes (the run's `outputs/result.json`).
  With `--latest`, the CLI first resolves the newest publication and then
  fetches the raw bytes **of that id**, so the reported `publication` and the
  bytes always belong together even if a newer result is published meanwhile.
  - Without `--output-file`, `raw` is `{written: false, bytes, sha256,
    contentType?, content?}`. `content` is present only when the bytes are text;
    more than 1 MiB fails with `SERVICE_RESPONSE_TOO_LARGE` and a `reason` asking
    for `--output-file`. Human mode prints just the text, like `cat`.
  - With `--output-file FILE`, the bytes are streamed to a **new** file (at most
    64 MiB) and `raw` is `{written: true, bytes, sha256, contentType?}`. The file
    must not exist; it is created before any request and removed again if the
    command fails, so the same command can simply be retried. The local path is
    never echoed in JSON.
  - `--output-file` without `--raw` is refused: only raw bytes are written.

## Publishing (owner only, confirm-class)

Publishing creates a public page, so both mutations require `--yes`. Without it
nothing is sent: the command exits 2 with `CONFIRMATION_REQUIRED`,
`details.reason` naming the consequence (publish puts the result on a public
page; unpublish removes that page and its link stops working), the planned
request (method, path, body digest) and copyable `confirmArgv`/`previewArgv`.
`--preview` prints the same plan and exits 0.

```sh
prose cli result publish haiku --run run_abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc1 --yes --json
prose cli result unpublish haiku AbCdEf012_-x --yes --json
```

- `SLUG` is a program in your own account (`haiku`, not `someone/haiku`; the
  latter is refused with the slug to use).
- The run must be a completed run of a saved revision of that program, it must
  have `outputs/result.json`, and the program must be public. The publication id
  is derived from the program and run, so publishing the same run twice returns
  the same publication (201 the first time, 200 afterwards).
- The service reports refusals by status; the CLI adds a `details.reason` with
  the fix. Every command in a reason (and the `Public:` line after a publish)
  is complete and copyable:

| Service answer | Code | `details.reason` tells you to |
| --- | --- | --- |
| 409 `program_not_public` | `SERVICE_WRITE_CONFLICT` | review `cli program visibility SLUG public --preview`; making a program public exposes its source, so the reason never includes `--yes` and leaves that decision to the owner |
| 409 `run_not_completed` | `SERVICE_WRITE_CONFLICT` | publish a completed run |
| 409 `program_ref_mismatch` | `SERVICE_WRITE_CONFLICT` | publish a run started with `cli run submit --from SLUG` (or `OWNER/SLUG` when the owner was given) |
| 409 `canonical_result_missing` | `SERVICE_WRITE_CONFLICT` | publish a run that wrote `outputs/result.json` |
| 404 `run_not_found` | `SERVICE_RESOURCE_NOT_FOUND` | find the run with `cli run list` |
| 404 (program) | `SERVICE_RESOURCE_NOT_FOUND` | check your programs with `cli program list` |

409 answers also carry the sanitized service text in `details.serviceMessage`.
A missing publication (`result show`, `result unpublish`) or a program that is
missing or private (`result list`) is `SERVICE_RESOURCE_NOT_FOUND` with a
`reason` pointing at `result list`.

Deleting a program removes all of its publications on the service side.
