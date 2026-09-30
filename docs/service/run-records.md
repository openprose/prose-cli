# Run records: list, show, download, share

`prose cli run list|show|download|share` read the runs of the account behind the selected key. They work for runs started anywhere (web app, API, `cli run submit`). Every command supports `--output human|json|jsonl` and never prompts.

```sh
prose cli run list --limit 5 --json
prose cli run list --limit 5 --before "$NEXT" --json
prose cli run show RUN_ID --json
prose cli run show RUN_ID --file outputs/result.json
prose cli run show RUN_ID --file outputs/report.pdf --output-file report.pdf
prose cli run download RUN_ID --output-dir ./run-out --json
prose cli run share RUN_ID --preview
```

## `run list`

`GET /runs?limit=N[&before=CURSOR]`, newest first. `--limit` is a whole number from 1 to 200 (default 20) and is checked before anything is sent; a whole number out of range suggests the nearest accepted value (`--limit 500` → `--limit 200`). `--before` takes the previous result's `nextBefore` verbatim; the cursor is opaque. The result carries `nextBefore` (`null` on the last page). An empty page is success (`"runs": []`, exit 0). An unknown cursor is `SERVICE_REQUEST_REJECTED` with the service's message. Paging is explicit: loop on `nextBefore` yourself.

Each run keeps the service's field names: `run_id`, `created_at`, `status`, `model`, and when present `environment` (the public id: `builtin` or `linux`), `price_cents`, `environment_price_cents`, `billing_status`, `program_ref` and `repositories` (`provider`, `owner`, `name`, `ref`, `writable`). Other fields are dropped. Human mode prints `run_id  status  model  created_at` per run (`No runs.` for an empty page) and a copyable `Next page:` line that is the full command: `prose cli run list [--limit N] --before CURSOR`. `--output jsonl` prints one `openprose.service-record/1` line per run and then one `openprose.service-page/1` line with `count` and `nextBefore`:

```sh
prose --output jsonl cli run list --limit 50 \
  | jq -r 'select(.schema == "openprose.service-record/1") | .record.run_id'
```

## `run show`

Without `--file` the result is `{runId, run}`, with the run record at `result.run` as in `run submit` and `run watch`: the summary fields plus `files` (always an **array of relative paths**), `has_patch`, and when present `usage` (`input_tokens`, `output_tokens`), `session_files` and `error`. Signed `file_urls` (they carry `tok=`) and `customer_id` are never output. Human mode prints `Run RUN_ID` and one `label: value` line per field (price in dollars, usage flattened, one file per line; `repository changes: yes` when the run left a patch), then `Next:` commands: `run watch` for a run that has not ended, `run show RUN_ID --file FIRST` and `run download RUN_ID`. A failed run's `error` prints mapped words (`The run hit its 16-step limit before finishing.`, `The run used up its budget.`, … from `cli/shared/fixtures/service/run-errors.v1.json`) or `The run failed on the service.`, never the service's stage names; with `PROSE_DEBUG=1` it keeps the service's text with control characters turned into spaces (at most 4096 characters, keys redacted). `billing_status` is `settled`, `settling` (charged, not final; human `billing: settling (the final price is being confirmed)`) or `unknown`.

With `--file PATH` the path must be listed in the manifest exactly (otherwise `INVOCATION_INVALID` pointing at `prose cli run show RUN_ID`; when exactly one listed file ends with the typed path, as `outputs/part6.md` does for `part6.md`, `details.suggestedArgv` uses it). The CLI reads the manifest and then `GET /runs/{id}/files/{path}` with the key; each path segment is percent-encoded.

- Without `--output-file`, the result is `{runId, path, file: {bytes, sha256, written: false, contentType?, content?}}`. `content` is present only for UTF-8 text without control characters other than tab, LF and CR. Files over 1 MiB are `SERVICE_RESPONSE_TOO_LARGE`, naming `--output-file`. Human mode prints the text exactly (like `cat`), or a one-line size and digest summary for binary data, never raw terminal escapes.
- With `--output-file FILE`, the bytes stream to FILE (up to 64 MiB), which must not exist; this is checked before anything is sent. The result has `written: true` and no `content`. On any failure the partial file is removed.

## `run download`

`--output-dir DIR` must not exist and its parent must; both are checked before any request (exit 2 otherwise). Without `--output-dir` the directory is `./RUN_ID`, with the same refusal when it already exists (the reason names the default and `--output-dir`); human output ends the summary line with the directory it wrote. The CLI then:

1. reads the manifest and validates every listed path: relative, no `..`, `.` or empty segment, no backslash or control character, no duplicates, no path that is both a file and a directory, and not the marker's name. Any violation is `SERVICE_PROTOCOL_INVALID` and **DIR is never created**;
2. creates DIR exclusively and streams each file into it with the key (`GET /runs/{id}/files/{path}`), never overwriting, at most 64 MiB per file, 512 MiB and 10,000 files per run (`SERVICE_RESPONSE_TOO_LARGE` otherwise);
3. writes `.prose-run-manifest.json` **last**: first under `.prose-run-manifest.json.partial`, then linked into place without replacing anything.

The result is `{runId, outputDir, fileCount, totalBytes, files: [{path, bytes, sha256}], marker}` in manifest order: `outputDir` is the directory as given and `marker` names `.prose-run-manifest.json`, the record written last. A run without files is success with `fileCount: 0` and only the marker.

**The marker is the completion signal.** If a download fails part way (a file 404, a disconnect, a cap), the error's `details.reason` says which file and that the directory is incomplete; files already written are kept and there is no marker. Pick a new `--output-dir` to retry. An existing destination (the default `./RUN_ID` or `--output-dir DIR`) is refused before anything is sent, and `details.suggestedArgv` names the first free `DIR-2` … `DIR-99`. The marker holds the canonical JSON (sorted keys, one line) of `{"download": <the result>, "run": <the run show manifest>}`, so `sha256` values can be checked offline.

## `run share`

`POST /runs/{id}/share` mints a public, unrevocable link to the run's artifacts that expires after 24 hours. It needs `--yes`; without it the command exits 2 with `CONFIRMATION_REQUIRED` and the planned request, and `--preview` prints the plan. Neither sends anything. The result is `{runId, url, expires_at?}`; `expires_at` is the link's own `exp` as an RFC 3339 UTC time. The URL appears only in the result: human mode prints it on stdout and a warning on stderr. The service does not check that the run exists before minting a link, so check the id with `run show` first.

## Errors

| Situation | Code | Exit |
| --- | --- | --- |
| Malformed run id, `--limit`, `--before`, `--file`; existing destination; `--output-file` without `--file` | `INVOCATION_INVALID` | 2 |
| `run share` without `--yes` | `CONFIRMATION_REQUIRED` | 2 |
| Run or file not found | `SERVICE_RESOURCE_NOT_FOUND` | 10 |
| Unknown cursor | `SERVICE_REQUEST_REJECTED` | 10 |
| Unsafe or conflicting manifest paths, malformed known fields | `SERVICE_PROTOCOL_INVALID` | 10 |
| A cap exceeded | `SERVICE_RESPONSE_TOO_LARGE` | 10 |
| No response headers within 30 s, or no body data for 45 s (the manifest's `download` class; applies to every file read, including `run show --file`), or a dropped connection | `SERVICE_UNAVAILABLE` | 10 |

## Tests

- Shared corpus: `cli/conformance/cases/service/run-records/` (`python3 cli/conformance/runner/service_operations.py --feature run-records -- <test-seam binary>`).
- Caps that fixtures cannot reach (64 MiB, 512 MiB, the 1 MiB inline limit): `cli/rust/crates/prose-cli/tests/service_run_records.rs` and `cli/bun/test/service-run-records.test.ts`. Test-seam builds only honor `PROSE_TEST_SERVICE_DOWNLOAD_LIMITS=<file bytes>,<run bytes>`, which can only lower the caps; ordinary builds ignore it.
- Transport timeouts: Bun `cli/bun/test/service-download-timeouts.test.ts` drives the real fetch path against a local server that stalls before headers, mid-body and mid-error-body (timeouts shortened on the Transport instance, no environment variable). The Rust port gets the same pair from ureq `timeout_connect`/`timeout_read`.
