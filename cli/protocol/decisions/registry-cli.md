# IMP-034 registry transport contract

Rust and Bun implement this protocol independently. The normative package format,
canonical bytes and hashes are in `shared/fixtures/registry/`. Neither implementation
interprets Markdown or executes package contents.

All requests use the selected account service origin and the fixed `/registry/v1/`
prefix. The existing environment selection and credential stores apply. Redirects,
arbitrary upload hosts and cross-environment credential fallback are prohibited.

## Commands

All commands accept trailing `--json` or the global `--output json` option.

- `cli package publish <source> --organization <slug> --name <slug> --version <semver> [--public]`
  publishes one regular file or an explicitly described directory. A single file
  uses its basename as the default export and has no dependencies. A directory
  requires a regular `prose-package.json` containing exactly `schema`, `files`,
  `exports` and `dependencies`; the schema is `prose-package-directory-v1`.
  Only listed files are read. Unknown or duplicate flags and manifest keys are
  rejected. Publication is private unless `--public` is supplied. The CLI posts
  canonical package JSON and accepts HTTP 200 or 201 only with a receipt matching
  the local identity, hash, inventory and visibility. It does not retry automatically.
- `cli package fetch <organization>/<package>@<version> --output-dir <fresh-directory> [--sha256 <digest>]`
  retrieves a receipt and then the artifact. It verifies the closed receipt shape,
  artifact hash, optional caller hash, canonical bytes and inventory before writing
  files. The destination must not exist. Exact file bytes and a
  `.prose-package-receipt.json` are written. Dependencies remain pinned metadata;
  this operation does not resolve or execute them.
- `cli package list <organization> [--cursor <cursor>]` retrieves one page of public
  receipts. The response contains `packages` and `nextCursor`. A cursor is at most
  210 ASCII characters and matches `public:[a-z0-9-]+:[0-9A-Za-z.+-]+`; it is encoded
  as a query value. Private packages remain available by authorized exact reference.
- `cli package withdraw <organization>/<package>@<version>` requires a credential
  and validates the returned receipt and `withdrawn: true`. Withdrawal removes
  discovery entries; it does not delete content or recall public downloads.

These commands reach the production OpenProse service only. Read
operations may proceed anonymously when no credential is available. Malformed
explicit or stored credentials fail rather than falling back to another identity.

## Filesystem guarantees

Paths, file counts and byte limits follow the normative package format. Source
symlinks, including ancestor symlinks, are rejected. No recursive upload, glob
expansion or implicit inclusion occurs. Unlisted files are ignored.

Rust installs a verified sibling staging directory with an atomic no-replace
rename on supported platforms. Bun reserves the destination with exclusive
`mkdir`, creates verified files exclusively, and writes the receipt last. The Bun
destination can be visible while it is populated; an interrupted destination
without a receipt is incomplete. Neither implementation overwrites an existing
destination. Cleanup verifies the identity of created entries and preserves
replaced or unexpected content. These checks do not provide a sandbox against a
malicious process with the same filesystem privileges.

## Results and errors

JSON output follows `package-operation.schema.json`: `schema`, `environment`,
`operation`, `result` and `problem`, with no extra fields. Successful fetch returns
only the receipt, not local paths. Structured output has no banner, and
`environment` is always `production` in public builds. Publishing and fetching do not
start a harness.

Invalid invocation and local configuration failures exit with code 2. Service
failures exit with code 10. HTTP 401/403 require authentication; 400/429 and
unavailable service responses report `SERVICE_UNAVAILABLE`; 404 reports
`SERVICE_RESOURCE_NOT_FOUND`, not retryable, naming the package version or
organization (amended with the hosted service operations); 409 reports
`SERVICE_PROTOCOL_INVALID`. Raw response bodies and credentials are never printed.
Requests and responses are bounded to 2 MiB with a ten-second client deadline.

## Test transport

Test builds support `PROSE_TEST_SERVICE_FIXTURE`. Fixtures specify the selected
environment, credentials and ordered exchanges containing method, path, status
and body. Artifact bodies are raw UTF-8 strings. Optional expected request bytes
and hashes verify publication. Unknown or exhausted exchanges fail without a
network fallback. Ordinary binaries ignore this fixture setting.

Shared process tests exercise both implementations using the same single-file
and directory vectors. Backend tests separately cover authorization, concurrent
publication, idempotent retries, conflicts and withdrawal. Hermetic tests do not
establish deployed Cloudflare behavior or live credential-store interoperability.
