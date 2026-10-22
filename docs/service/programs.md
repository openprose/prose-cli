# Saved programs

A **saved program** is a `.prose.md` file stored on the OpenProse service under your GitHub
handle, at `OWNER/SLUG`. Every save that changes the text creates a new
revision; revisions are content-addressed (`rev_id`) and chained by save
(`commit_id`, `parent_commit_id`). Programs are private until you make them
public. The commands are service
operations and behave the same in the Rust and Bun builds.

| Command | Request | Key | `--yes` |
| --- | --- | --- | --- |
| `prose cli program list [--with-content]` | `GET /programs` | yes | no |
| `prose cli program show [OWNER/]SLUG[@REV] [--output-file FILE]` | `GET /p/{owner}/{slug}[@rev]` | optional | no |
| `prose cli program save SLUG FILE\|- [--message M] [--base COMMIT_ID]` | `PUT /programs/{slug}` | yes | no (`--preview` works) |
| `prose cli program visibility SLUG public\|private` | `PUT /programs/{slug}/visibility` | yes | yes |
| `prose cli program delete SLUG` | `DELETE /programs/{slug}` | yes | yes |
| `prose cli program revisions SLUG` | `GET /programs/{slug}/revisions` | yes | no |
| `prose cli program copy [OWNER/]SLUG[@REV] --to ORG [--slug NEW]` | `POST /p/{owner}/{slug}[@rev]/copy` | yes | yes |
| `prose cli program draft SENTENCE [--current FILE] [--model M] [--output-file FILE]` | `POST /write` (paid) | yes | yes |

Add `--json` (or `--output json|jsonl`) for machine output; every result is an
`openprose.service-operation/1` envelope whose `result` matches
[`programs.schema.json`](../../cli/shared/schemas/service/programs.schema.json).

## Save and read

```sh
prose cli program save hello hello.prose.md --message "first" --json
prose cli program show alice/hello --output-file copy.prose.md --json
prose cli program revisions hello --json
```

- `save` sends exactly `{content, message?, base_commit_id?}`. It **never sends
  visibility**, so saving can never publish a program; new programs are private.
- The bytes are sent and returned unchanged (CRLF stays CRLF). `FILE` may be
  `-` for standard input; it must be UTF-8, non-empty and at most 262,144 bytes
  (the service limit), checked before anything is sent.
- `--message` is trimmed and must be 1 to 160 characters without control
  characters. `--base COMMIT_ID` makes the save conditional: if someone saved a
  newer revision, the command fails with `SERVICE_WRITE_CONFLICT` (exit 10) and
  `details.currentCommitId` / `details.currentRev` name the current revision, so
  you can reconcile and retry with `--base <currentCommitId>`.
- Saving identical text returns the same `rev` and `commit_id`.
- A 403 is classified by the service's message: no linked GitHub login (your
  handle) is `GITHUB_LINK_REQUIRED` (the key itself is valid); an invalid or
  revoked key is `SERVICE_AUTH_REQUIRED`; a reserved handle or a slug owned by a
  different account is `SERVICE_REQUEST_REJECTED` with `details.serviceMessage`.
- The file bytes are sent exactly, including a leading UTF-8 byte-order mark.
- `show` returns `{program, is_owner, file}`. `program` is the metadata (no
  text). `file` is `{bytes, sha256, written, content?}`: without
  `--output-file`, `content` holds the text (omitted if it contains control
  characters other than tab, LF and CR); with `--output-file` the exact bytes go
  to a **new** file (checked before the request; never overwritten) and the
  local path is never echoed. Human mode prints the text itself, like `cat`.
- `OWNER/SLUG@REV` reads one revision by its 16-hex `rev_id` (from
  `revisions`). Without `@REV` the latest revision is read. A bare `SLUG` is
  your own program (its owner is read from `GET /programs/{slug}/revisions`).
- `@REV` may also be a revision **number** of your own program (`hello@1`, the
  `rev` column): it is resolved through the same revisions list to its
  `rev_id`, which the result reports as `program.ref`; human mode also prints
  `Resolved hello@1 to alice/hello@<rev_id> (rev 1).` on stderr. Another
  owner's program has no revision listing, so its `@N` exits 2 with the
  command that prints the rev_id; a number the program does not have exits 2
  naming the newest revision.
- Every program record (`list`, `show`, `save`, `visibility`, `revisions`)
  carries `ref`, the pinned `OWNER/SLUG@rev_id` that `run submit --from` and
  `job contract attach` take. Human `list` and `revisions` lines label their
  columns: `alice/hello  rev 2  private  rev_id=…  ref=alice/hello@…` and
  `rev 2  rev_id=…  commit_id=…  ref=…`. Every program record carries
  `updated_at` (epoch ms) and `updated_at_iso` (RFC 3339 UTC); `--output
  jsonl` prints one record line per program or revision and a page line
 .
- `show` sends your key when one is configured, so you can read your private
  programs. Without any key it reads anonymously, which works for public
  programs. A missing or private program is `SERVICE_RESOURCE_NOT_FOUND`; the
  service does not reveal which.
- `list` is newest first and omits the text unless `--with-content` is given.
  Platform programs are not listed.

## Your own programs: `SLUG` or `OWNER/SLUG`

`save`, `visibility`, `delete` and `revisions` (and `result publish|unpublish`)
act on your own programs. They take the bare `SLUG`, or `OWNER/SLUG` exactly
as `program list` prints it when `OWNER` is you. The CLI checks
`OWNER` against the owner that `GET /programs/{slug}/revisions` names (your
revisions only; letter case is ignored) before anything else is sent:

- another owner exits 2 with `OWNER "bob" is not you: your program "demo" is
  alice/demo, …; pass "demo"`, and nothing is written;
- a first `save` of a slug you have never saved cannot check `OWNER`, so it
  exits 2 asking for the bare slug instead of writing to your namespace;
- the other verbs exit 2 too when you have no such program: `OWNER "bob" is
  not confirmed as you: you have no saved program "demo" to check it against,
  …`. The Action names the bare slug and `cli program list`
  (`suggestedArgv`), nothing is sent, and `--preview` is refused the same way
 . Pass the bare slug to act on your own program; another
  owner's program cannot be changed from your account.

When `OWNER` is confirmed, the plan says so: the `--preview` result and
`CONFIRMATION_REQUIRED` `details.plannedRequest` carry `"owner":
{"handle":"alice","verified":true}`, and human output prints `Owner: alice
(verified as you)`.

A malformed slug is described in words (`lowercase letters, digits and
hyphens, at most 64 characters`) and an upper-case one names the lower-case
slug to pass. `VISIBILITY` is accepted in any letter case (`PUBLIC`); a typo
names the nearest value (`did you mean public?`), and `details.suggestedArgv`
is the same command with that value.

## Publish, unpublish, delete

```sh
prose cli program visibility hello public --yes
prose cli program visibility hello private --yes
prose cli program delete hello --preview      # prints the planned request, sends nothing
prose cli program delete hello --yes
```

Without `--yes` these exit 2 with `CONFIRMATION_REQUIRED`, send nothing, and
return the planned request plus the exact command to confirm
(`details.confirmArgv`) or preview (`details.previewArgv`). Making a program
private also removes its published results (see [results](results.md)).

`program delete SLUG --yes` of a program that is already gone exits 0 with
`{slug, deleted: true, alreadyAbsent: true}`. `OWNER/SLUG`
never reaches that answer unless `OWNER` was confirmed as you (see above). Any other program
404 is `SERVICE_RESOURCE_NOT_FOUND` with `details.resource` `{kind: "program",
id}` and `details.suggestedArgv` `cli program list`.

## Organization workspaces: `--org ORG` and `program copy`

You work either in your personal workspace (your own namespace, the default)
or in an organization's. `list`, `show`, `save`, `visibility`, `delete` and
`revisions` take `--org ORG` (the organization's slug, as `cli org list` prints
it) to act in that organization's namespace, `ORG/SLUG`. Without `--org`
nothing changes: the commands act on your personal programs.

```sh
prose cli program list --org acme --json
prose cli program save hello hello.prose.md --org acme
prose cli program visibility hello public --org acme --yes
prose cli program copy hello --to acme --preview
prose cli program copy hello --to acme --yes
```

- Who can read a program: a private personal program only you; a private
  organization program every member of that organization; a public program
  everyone.
- What a member can do depends on their role: a **reader** reads and runs,
  a **developer** also creates, edits and copies programs in, and an
  **admin** also changes visibility and deletes. Runs are billed to whoever
  starts them, as before.
- With `--org`, a bare `SLUG` names the organization's program, and
  `OWNER/SLUG` is accepted when `OWNER` is the organization (letter case
  ignored); another `OWNER` exits 2 naming the bare slug. No owner lookup is
  sent. The plan (`--preview`, `CONFIRMATION_REQUIRED`) carries
  `plannedRequest.organization`, and human output prints `Organization: ORG`.
- Before `save`, `visibility` or `delete` with `--org` (`--preview`
  included), the CLI reads `GET /programs?workspace=ORG`. The service must
  echo the organization; this also proves you are a member. A service that
  does not echo it (one that ignores `?workspace=` would list your own
  programs) gets `SERVICE_FEATURE_DISABLED` and nothing is written, so an
  organization command can never change a personal program. `list --org`
  refuses a listing without the echo the same way, and `revisions --org`
  refuses revisions owned by anyone but the organization.
- `program copy SOURCE --to ORG` copies a program into an organization.
  `SOURCE` is your own personal `SLUG[@REV]` (its owner is read from its
  revisions) or any `OWNER/SLUG[@REV]` you can read; without `@REV` the latest
  revision is copied. `--slug NEW` saves the copy under another slug. The
  copy is private to the organization's members, records its source as
  `copied_from`, and leaves the original unchanged. Copying shares the
  program's text with every member, so it needs `--yes` (or `--preview`),
  like `visibility`. The plan summary names `to` (and `slug`). A slug the
  organization already has is `SERVICE_WRITE_CONFLICT` (`serviceCode`
  `program_exists`) with the service's message.
- Program records may carry `owner_kind` (`personal` or `organization`) and
  `copied_from` (`{owner, slug, rev_id, ref}`; a malformed source is
  dropped, never fatal). Human `list` lines add
  `owner_kind=` and `copied_from=` columns, and human `show` prints
  `Owner: ORG (organization)` and `Copied from: REF` on stderr, so stdout stays
  the program text.
- Errors in an organization's namespace. The service answers a non-member
  and an unknown organization with the same 404, and a non-member and a
  missing program alike, so the CLI says which cases a 404 can mean:
  - `list --org` and the confirmation read of `save`, `visibility` and
    `delete`: the organization was not found, or you are not a member
    (`SERVICE_RESOURCE_NOT_FOUND`, `details.resource` `{kind:
    "organization", id}`, `cli org list` as `details.suggestedArgv`);
  - `show`, `revisions` and `visibility` with `--org`: the organization has
    no such program you can read, or you are not a member (`{kind:
    "program", id: "ORG/SLUG"}`, `cli program list --org ORG`). A confirmed
    `delete --org` of a missing program exits 0 with `alreadyAbsent: true`,
    because membership was confirmed first;
  - `copy`: the source was not found, or you are not a member of the target;
  - a role that does not allow the verb is `SERVICE_REQUEST_REJECTED`
    (`serviceCode` `organization_forbidden`) whose reason names the role the
    verb needs, with `details.requiredRole`;
  - an organization that cannot hold programs (for example the personal
    organization named after your login, or a reserved name) is
    `SERVICE_REQUEST_REJECTED` (`serviceCode`
    `workspace_namespace_unavailable`) with the service's message. A slug
    longer than 39 characters (a program owner's limit) exits 2 before any
    request;
  - a service without organization programs answers `--org` with
    `SERVICE_FEATURE_DISABLED`; nothing falls back to your personal programs.
- `--org` and `--to` take a lowercase slug; an upper-case slug exits 2 naming
  the lowercase one. `--workspace` suggests `--org` (or `--to` for `copy`),
  and `program cp` suggests `program copy`.
- There is no `PROSE_ORG` environment default. A namespace that silently
  redirected `save`, `visibility` and `delete` into a shared organization would
  be easy to miss, and an environment variable would suggest that `run`,
  `job` and `wallet` follow it too. Pass `--org` on each command.

## Draft a program with the hosted writer (paid)

```sh
prose cli program draft "A function that returns a haiku about the input topic" --output-file haiku.prose.md --yes --json
prose cli program draft "Also return a title" --current haiku.prose.md --output-file haiku-v2.prose.md --yes --json
```

- The writer is a hosted run and is charged like one (a short draft costs a
  few cents). It reserves a hold like a run: the plan (`--preview` or
  `CONFIRMATION_REQUIRED`) carries `plannedRequest.quote` from the anonymous
  `GET /run/quote` (advisory; a failed quote never hides the plan), and a
  `--model` is checked against `GET /models` before the gate (an unknown name
  lists the nearest offered models). The price is known only
  after the run: the result carries `runId`, `price_cents`,
  `environment_price_cents` and `billing_status` copied from the service, and
  the run also appears in `cli run list` (`program_ref` `openprose/prose-write@…`).
- The CLI waits on the service's event stream, so a draft of several minutes is
  fine; if the connection drops after the run started, the error names the run
  (`details.runId`) for `cli run show`. Nothing is ever resubmitted
  automatically. Ctrl-C stops waiting (`CANCELLED`, exit 24); the draft run
  continues on the service.
- A failed or empty draft is `HOSTED_RUN_FAILED` (exit 22) with `details.runId`.
- The result is `{file, runId, price_cents, environment_price_cents,
  billing_status}`. `file` holds the drafted `.prose.md` program (the service's
  JSON-string result is decoded, then trimmed to end in one LF), or it is
  written to `--output-file` (a new file, checked before the paid request).
- `--current FILE` sends an existing program to revise (at most 262,144 bytes).
  `--model` is passed through, but the current service chooses the authoring
  model itself.
- `SENTENCE` must be non-blank, at most 4,096 characters, without control
  characters other than tab and LF.
