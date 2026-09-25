# Organizations

`prose cli org …` manages OpenProse organizations (`org list` is an account
command). Organizations are available to admitted
accounts only. An account that is not admitted gets `SERVICE_FEATURE_DISABLED` (the service answers 404
`feature_disabled`), not "not found".

| Command | Request | Result (`result` of `openprose.service-operation/1`) | `--yes` |
| --- | --- | --- | --- |
| `cli org list` | `GET /organizations` | `{organizations}` (see below) | |
| `cli org show ORG` | `GET /organizations/{ORG}` | `{organization}` | |
| `cli org create SLUG [--name NAME]` | `POST /organizations` `{slug, name?}` | `{organization}` | yes |
| `cli org rename ORG NAME` (or `--name NAME`, as `org create` spells it) | `PATCH /organizations/{ORG}` `{name}` | `{organization}` | yes |
| `cli org default ORG` | `PUT /organizations/default` `{organization}` | `{organization}` | yes |
| `cli org member list ORG` | `GET /organizations/{ORG}/members` | `{members: [member]}` | |
| `cli org member role ORG ACCOUNT_ID ROLE` | `PATCH …/members/{ACCOUNT_ID}` `{role}` | `{member}` | yes |
| `cli org member remove ORG ACCOUNT_ID` | `DELETE …/members/{ACCOUNT_ID}` | `{ok: true}` | yes |
| `cli org invite ORG ACCOUNT_ID --role ROLE [--expires-in SECONDS]` | `POST …/invitations` `{accountId, role, expiresInSeconds?}` | `{invitation, token}` | yes |
| `cli org invitation revoke ORG INVITATION_ID` | `DELETE …/invitations/{INVITATION_ID}` | `{ok: true}` | yes |
| `cli org invitation accept --token-file FILE\|-` | `POST /organizations/invitations/accept` `{token}` | `{organization}` | yes |

There is no `org delete`: the service refuses every organization deletion
(409 `organization_deletion_unsupported`) until a retention policy exists.

`org list` is an account command with its own parser, but it prints the same
`openprose.service-operation/1` envelope as every other `org` command
(`operation` `org.list`, `result` `{organizations}` with `id`, `slug`, `name`
and `role`, no `created_at`); `--output jsonl` prints one
`openprose.service-record/1` line per organization and the page trailer. Every
`org` command has its own `--help`. The nested groups
have help too: `cli org member --help` and `cli org invitation --help`.

## Inputs

- `ORG` is an organization slug or id. It is sent as one percent-encoded path
  segment. `default` means the caller's default organization **only** for
  `org show`. Every other command needs the real slug; a not-found for the
  literal `default` says so in `details.reason`:

  ```sh
  slug=$(prose cli org show default --json | jq -r .result.organization.slug)
  prose cli org member list "$slug" --json
  ```

- `ORG` and `SLUG`: 1 to 256 characters. `ACCOUNT_ID` and `INVITATION_ID`:
  1 to 128 characters, the result schema's cap. The service would accept a
  longer account id and create the invitation, and then the CLI could not
  project the echo. `NAME` and `--name`: 1 to 200 characters. None may
  contain control characters. Anything else is `INVOCATION_INVALID` and
  nothing is sent.
- `MEMBER` (`org member role|remove`) is a member number as `org member
  list` prints it (`2` or `"member 2"`; the list is read first to find that
  member), or the member's account id.
- An argument that becomes a URL path segment (`ORG` outside `org default`,
  `MEMBER`, `INVITATION_ID`) cannot be `.` or `..`.
  URL normalization drops those segments, so the request would reach a
  different route from the one planned. `org create` checks the service's
  slug rule before any request (1 to 63 lowercase letters, digits or interior
  hyphens; `openprose`, `system`, `default`, `invitations` and UUID-shaped
  slugs are reserved): a slug outside it is `INVOCATION_INVALID`, and when a
  valid slug can be derived (lower-cased, other characters one hyphen)
  `details.suggestedArgv` is the same command with it. A slug
  the service still refuses is `SERVICE_REQUEST_REJECTED` carrying its
  `serviceMessage`. Plans carry `plannedRequest.summary` (`slug`, `name`,
  `role`), and `CONFIRMATION_REQUIRED` says in `details.reason` what `--yes`
  would do (a slug is global and permanent).
- `ROLE` and `--role`: `admin`, `developer` or `reader`.
- `--expires-in`: whole seconds from 1 to 2592000 (30 days). Without it the
  service uses 7 days.
- The invitation token is never taken from argv. `--token-file` reads it from
  a file, or from standard input with `-`, at most 4096 bytes, as one line
  (one trailing LF or CRLF is dropped). It is never printed, and a plan shows
  only its SHA-256 digest.

## Confirmation

Every mutation is confirm-class (the catalog's agent policy for
`organizations.manage` is `never`), and no command ever prompts. Without
`--yes`, a mutation exits 2 with `CONFIRMATION_REQUIRED` and sends nothing.
`details.plannedRequest` holds the method, a description, the body digest and effect, and
`details.confirmArgv` / `details.previewArgv` hold the exact commands to rerun.
`--preview` prints the same plan and exits 0. Neither needs an API key.
The JSON plan carries only the body's digest (the frozen `plannedRequest`
shape) and `confirmArgv` carries the values. The human `--preview` names
what the request acts on in plain lines (`Organization:`, `Member:`,
`Account:`, `Expires in:`, `Invitation:`) next to its `Summary:` (slug, name,
role); it never prints the raw request body, and `invitation accept` never
shows its secret token. `org create` says it cannot be undone: slugs are
global and permanent.

## Results

Projections are closed ([`service/organizations.schema.json`](../../cli/shared/schemas/service/organizations.schema.json)):

- `organization`: `id` (UUID), `slug`, `name`, and when the service sends them
  `created_at` (epoch ms) and `role` (the caller's role; absent on `create`).
  `created_by` is dropped.
- `member`: `member` (`member N`, numbered by when members joined; in `org
  member role`, the account id when the caller named one), `role`,
  `created_at`, and `handle` when the service sends one. Account ids and the
  organization id are not printed; human `org member list` prints
  `member N [(handle)]  role`.
- `invitation`: `id`, `account_id` (the invitee the caller named), `role`, `created_at`,
  `expires_at`, `consumed_at`, `revoked_at` (the last two are `null` until
  used). The one-time `token` sits next to it and appears only in the `org
  invite` result.
- Every epoch-ms time (`created_at`, `expires_at`, `consumed_at`,
  `revoked_at`) also appears as `<name>_iso` (RFC 3339 UTC, or null), and
  human `org invite` prints the expiry that way. An empty human `org member
  list` says `No members in ORG.`. Give it to the invitee through a private channel; the invitee
  runs `prose cli org invitation accept --token-file FILE --yes` with their own
  key.

A success body that does not fit these shapes, for example a name containing
a control character or an unknown role, is `SERVICE_PROTOCOL_INVALID`.
`details.reason` names the field (`organization.name`, `members[].role`, …) and
the value is never printed.

## Errors

| Service answer | Code | Exit |
| --- | --- | --- |
| 404 `feature_disabled` (not available for this account) | `SERVICE_FEATURE_DISABLED` | 10 |
| 404 `organization_not_found`, `member_not_found`, `invitation_not_found` | `SERVICE_RESOURCE_NOT_FOUND` | 10 |
| 403 `organization_forbidden` (admin role required) | `SERVICE_REQUEST_REJECTED` | 10 |
| 403 without a code | `SERVICE_AUTH_REQUIRED` | 10 |
| 409 `slug_taken`, `last_admin`, `already_member` | `SERVICE_WRITE_CONFLICT` (with `serviceMessage`) | 10 |
| 400 `invalid_slug`, `invalid_name`, `invalid_role`, `invalid_invitation`, `invalid_expiry`, … | `SERVICE_REQUEST_REJECTED` (with `serviceMessage`) | 10 |

`serviceCode` is present only for allowlisted codes. `member_not_found`,
`invitation_not_found`, `last_admin`, `already_member`, `invalid_invitation`
and `invalid_expiry` are not yet in the allowlist, so for those only the
status, and for 400 and 409 the `serviceMessage`, identify the cause. To keep
the cases distinct anyway, the org commands add `details.reason`:

- A not-found on `member role`/`member remove` that is not
  `organization_not_found` says the ORG has no member with that ACCOUNT_ID.
- The same on `invitation revoke` says there is no invitation with that
  INVITATION_ID.
- A rejected `invitation accept` says the token is invalid, expired, revoked,
  already used, or was issued to a different account.

An `organization_not_found` keeps its `serviceCode` and gets no hint, because
the ORG is what is wrong there.

## Examples

```sh
prose cli org show default --json
prose cli org member list acme-research --output json
prose cli org invite acme-research ACCOUNT_ID --role reader --expires-in 86400 --preview
prose cli org invite acme-research ACCOUNT_ID --role reader --expires-in 86400 --yes --json
prose cli org invitation revoke acme-research 7e6d5c4b-3a29-4817-8f6e-5d4c3b2a1908 --yes
prose cli org invitation accept --token-file ./invite.token --yes
```
