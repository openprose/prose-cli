# IMP-026 — Explicit local weave host bridge, version 1

Status: proposed isolated-branch behavior; not released, not publication authority.
The shared fixture is `cli/shared/fixtures/weave-host-v1.json`. Both products must
implement this contract independently. The bridge is a process carrier, not a
second weave engine, installer, language dispatcher or provider adapter.

## Grammar and routing

```
prose cli weave --help
prose cli weave --host-binding ABS check CONFIG
prose cli weave --host-binding ABS status CONFIG
prose cli weave --host-binding ABS step CONFIG
prose cli weave --host-binding ABS serve CONFIG --poll-ms N --max-steps N
prose cli weave --host-binding ABS settle CONFIG --binding VALUE --attempt VALUE --outcome completed|not-applied --receipt VALUE
```

Order is exact; no aliases, equals forms, repeated flags, extra tokens or suffix
`--json`. `ABS` and `CONFIG` are nonempty absolute paths without NUL, at most
4096 UTF-8 bytes. CONFIG need not exist: the coordinator owns its diagnostics.
Settlement strings are 1..4096 UTF-8 bytes, without NUL and not wholly Unicode
White_Space or U+FEFF. Serve numbers use canonical positive ASCII decimal with
no leading zero: poll 1..3600000 ms, max steps 1..1000000. Settlement identity
and outcome truth remain the coordinator/caller's responsibility.

All runner-global flags supplied before `cli` are rejected for weave (including
output, cwd, dry-run, model, timeout and no-color). Existing global --help and
--version short-circuit behavior is unchanged. Invalid weave grammar fails before
opening a binding or starting a process. Help needs no binding and starts nothing.
The bridge owns no top-level language token: `weave ...`, `init ...`, and escaped
`-- cli weave ...` retain the existing opaque forwarding rules.

## Dedicated host binding

Exactly these JSON fields are required:

```json
{"schema":"openprose.weave-host-binding/1","executable":"/absolute/weave-local","sha256":"64 lowercase hexadecimal characters","environmentKeys":["PATH","HOME","OPENAI_API_KEY"],"timeoutMs":900000,"maxOutputBytes":1048576}
```

Reject unknown or duplicate keys (including escaped duplicate spellings), BOM,
invalid UTF-8, unpaired surrogate escapes, nonfinite numbers, and noncanonical
integer tokens (fractional/exponent/negative-zero forms included). The file must
be regular and at most 65536 bytes; use nonblocking bounded reads to reject FIFO,
device and growing oversized files without hanging. No binding discovery or
runner TOML/environment fallback exists. Parent symlinks are trusted local path
resolution; canonicalize the selected binding and use its parent as child cwd.

Executable is an absolute nonempty NUL-free path of at most 4096 UTF-8 bytes.
Resolve it to a regular executable file, cap hashing at 536870912 bytes, and verify
its complete SHA256 against exactly 64 lowercase hexadecimal characters before
launch. Launch the resolved path. No PATH lookup, shell, PTY, script interpreter
selection or automatic download. The selected file is trusted executable input;
a shebang's interpreter and concurrent filesystem replacement are not attested.
This is a sequential trusted-local check, not atomic execution identity or a sandbox.

`environmentKeys` is an array of at most 128 unique names matching
`[A-Za-z_][A-Za-z0-9_]{0,127}`. Build an empty environment and copy only present
selected names from the caller; missing names remain absent. Do not fail missing
credentials in the bridge: check/status/settlement must remain usable without them.
Do not inject defaults, recursion markers or additional environment values.

`timeoutMs` is an integer 1..86400000; `maxOutputBytes` is 1..16777216.
No default or zero/unbounded values. Validate all fields and executable admission
before starting a child. Failed binding admission never creates host/checkpoint
state. Existing weave config and provider policies remain independent.

## Streams, exits and bounded execution

The child argv is exactly OP, CONFIG and the operation suffix shown above.
Stdin is immediately closed. Forward stdout and stderr bytes to their respective
streams without decoding, JSON validation, newline insertion or result wrapping.
Preserve each stream's order; no cross-stream ordering is promised. A normal exit
0..255 is returned unchanged, including check's exit 2. Native signal termination
returns 128+signal. A successful child exit does not establish fulfillment.

Count all captured stdout plus stderr bytes against one budget. Forward at most
the remaining budget; observing any further byte stops the child with OUTPUT_LIMIT.
The wrapper's fixed failure diagnostic is separate from this child-byte budget.
Use bounded chunks/queues, drain both streams, and never collect unbounded output.
A child's early exit does not allow unlimited waiting for inherited open pipes:
the same deadline continues through draining. Deadline starts immediately before
spawn. Binding/hash admission is bounded by bytes, not this child deadline.

Before spawn, install cancellation observation without a signal race. SIGINT and
SIGTERM request termination of the active owned process group. Timeout, output
limit or forwarding/read failure do the same. Send SIGTERM first; allow at most
1000 additional ms, then SIGKILL and reap the direct child. Explicit cancellation
returns 130 for SIGINT or 143 for SIGTERM. First observed failure wins; signal arrival order before runtime observation is not guaranteed. If both signal flags are pending at the first observation, a polling implementation selects SIGINT. Once observed, cancellation identity cannot change. Never send
a group signal after direct-child exit/reaping (PID reuse risk). Stop draining at
the bound/deadline, close pipes and avoid orphaned reader tasks. If a child already
exited but another process holds pipes, close them and report the applicable bound
without signaling the exited child's group. Broken wrapper output is IO_FAILED.

Unix only for this first profile: non-Unix execution fails before any spawn.
Processes must start in their own owned process group. This is best-effort local
supervision, not containment: descendants that create other groups/sessions may
survive forced termination. Graceful host cancellation can preserve/release its
own state; forced interruption may retain conservative locks and pending effects.
Never clear locks, infer completion, settle, reset budgets or replay on any bridge
failure. The operator uses the host's existing explicit recovery contract.

## Fixed wrapper failures

For an admitted weave route, failures write exactly the following ASCII code plus
LF to stderr, and no wrapper-authored stdout. Previously forwarded child bytes
remain visible; wrapper diagnostics never include paths, argv, binding contents,
environment names/values or child exception text. If wrapper stderr itself is blocked or closed, diagnostic delivery is best effort with a 100 ms bound; the exit code remains authoritative for wrapper failure. The runtime must not hang to print a diagnostic. Child-authored streams are not
redacted; callers must select a trusted host that implements its own output policy.

| Category | Exit |
| --- | --- |
| WEAVE_HOST_INVOCATION_INVALID | 2 |
| WEAVE_HOST_BINDING_INVALID | 2 |
| WEAVE_HOST_UNSUPPORTED_PLATFORM | 2 |
| WEAVE_HOST_START_FAILED | 126 |
| WEAVE_HOST_TIMEOUT | 124 |
| WEAVE_HOST_OUTPUT_LIMIT | 125 |
| WEAVE_HOST_IO_FAILED | 125 |
| WEAVE_HOST_CANCELLED | 130 or 143 |

Syntax failure before the CLI recognizes the weave route retains existing runner
INVOCATION_INVALID behavior. Within a valid weave route, check syntax first, then
platform, then binding, then spawn. Normal nonzero child exit is not a wrapper
failure and adds no diagnostic. No automatic retry exists.

## Qualification and scope

Use fresh roots, actual temporary fake executables, empty/selected environments,
no network/providers and bounded test deadlines. Fixtures describe deterministic
inputs and outputs; the root-owned runner expands paths and executable hashes.
Include byte-invalid UTF-8 output, concurrent stderr, exit propagation, admission
failures, escaped language routing, timeout, signal, inherited-pipe and saturation
cases. Signal cases assert bounds and state preservation, not exact scheduling.
Then delegate to both real compiled local coordinators for repair/reuse/pending/
settlement checks. Claim neither public distribution nor model reliability from
these tests. SDK dependencies remain outside the CLI product boundary.
