# Hosted client corrective qualification — September 30, 2026

This corrective branch starts from hosted-client PR 10 at `510305c`. It changes
source only; no service deployment, account mutation, package release, or paid
model execution is part of qualification.

## Proxy behavior

The pinned Bun 1.3.5 runtime's native proxy selection fails the shared vectors,
including empty lowercase variables and bypass lists. The public CI failure
reached the service directly despite an unrelated `NO_PROXY` list with a trailing
empty entry. Rust already applies the specified rules independently.

Bun now selects the same shared policy explicitly and uses the exact Undici
7.30.0 raw HTTP transport. The package's explicit `undici/index.js` entry avoids
Bun's native built-in substitution. This new dependency is necessary to this
feature correction; the Bun runtime and existing dependencies are unchanged.
The transport keeps native streaming, caller-controlled cancellation, response
limits, manual redirects, and error classification. HTTP requests are marked
non-idempotent at the transport layer to prevent automatic replay. Failed proxy
CONNECT attempts become terminal errors rather than repeated connections.
The shared installed-process probe additionally requires exactly one proxy
connection and exercises empty lowercase fallback.

Rejected local prototypes included using Bun's Node HTTP compatibility layer
and changing process proxy variables; neither enforced all pinned rules. An
Undici fetch prototype failed streaming qualification and was replaced by raw
request/stream handling. These failures were not counted as passing evidence.

## CodeQL alert 3

The actual SARIF trace for `rust/cleartext-logging` ends at `Vec::insert` in
`session_resubmit_argv`, where `--detach` is added to an argument vector. That
operation does not write a log. The recovery command deliberately includes the
run session so the same submitted run can be recovered without creating another
paid run. The current source constructs that vector by chaining slices, making
the operation explicit. A fresh CodeQL analysis must confirm the alert's status;
this local review does not claim that the GitHub alert is cleared or dismissed.

The local recovery journal is separate from this alert. Its supported Unix
implementation uses private directories and files; existing tests check those
permissions. No credential value or journal is copied into this record.

## Historical qualification

The former validation directories now have portable links to unchanged evidence
at immutable revision `6394f88`. Logs and inventories remain at that revision;
there is no history rewrite. Old evidence does not qualify the new source.

## Verification and limits

Local macOS ARM64 checks passed: Bun type checking; 106 hosted/account/registry
unit tests; all 22 proxy vectors; streaming, download and cancellation tests;
1,089 service cases against each compiled Rust and Bun product, plus
identity/example/proxy/determinism probes (including exactly one connection
for proxied requests); 21 registry process cases against each product; 98 Rust service tests; Rust formatting; architecture
boundaries and their 34 tests; and dependency contract tests (8).

The full local admission command stops at architecture-tests with 45 errors
from missing legacy workflow fixtures. The unchanged main `6394f88` reproduces
the same 45 errors. Those workflows were intentionally not migrated; restoring
them solely to satisfy obsolete tests is outside this correction. Full local
admission is therefore not green.

Fresh remote CI, CodeQL, other-platform installed qualification, and live service
qualification remain separate gates. The prototype failures above are retained
as limitations, not counted in these passing results.
