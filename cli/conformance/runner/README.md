# Shared black-box runner

Build both products with the explicit sentinel/test-seam profile and run the
admitted Phase-1 corpus:

```sh
python3 cli/conformance/runner/run.py --build --phase 1
```

The runner creates a distinct workspace, home, configuration, cache, temporary,
and empty executable-search root for each product in every case. A Rust run can
therefore never seed filesystem state for the corresponding Bun run. Provider
credentials are never inherited and network proxies are poisoned.

`--build` never consumes whichever ordinary or release-profile product happens
to be present in the repository output paths. It rebuilds the test-only
sentinel profile explicitly, captures each candidate into a private read-only
snapshot, and executes only those bytes. The direct signal oracle applies the
same build-and-snapshot rule, so the standalone runner suite remains valid even
after an `echo-v0` or functional-alpha release build.

Before differential comparison, each reported cwd path and identity is checked
against that product's allocated canonical workspace. Only that exact root and
its descendants are then rewritten to `{{WORKSPACE}}`; unrelated paths are not
normalized. Outputs are otherwise compared after only declared runner/clock
volatility is removed.

Every ordinary invocation is launched without a shell in an explicitly owned
process boundary. Stdout and stderr are drained concurrently with a fixed
4-MiB capture ceiling per stream; excess bytes are discarded while the pipe is
still drained, and truncation is retained as a validation failure. Timeout,
interrupt, and other `BaseException` paths clean the known boundary before
returning or re-raising.

On POSIX the wrapper starts a new session; timeout cleanup sends TERM and then
KILL to that exact original process group, reaps the wrapper, and settles both
output pipes. A timeout or nominal zero exit with a remaining original group
cannot be reported as success. This is not strict descendant containment: a
hostile child can call `setsid()` and leave the group. Such a detached child
may be invisible if it also closes inherited pipes, so every report fixes
`detachedDescendantContainment` to false. On Windows,
`CREATE_NEW_PROCESS_GROUP` is not Job Object authority; this runner cannot award
Windows descendant settlement. Product-side Job Object work remains
release-blocked pending native exact-package evidence.

Phase-2 transport cases are admitted only after both products expose the
test-only fake-process route. Real provider runs never execute here.

The Phase-7 DX oracle adds exact dry-run authorities and JSONL lifecycle cases
without changing the already-admitted Phase-6 set:

```sh
python3 cli/conformance/runner/run.py --phase 7
```

Dry-run JSON must match the shared fixture byte model after only the allocated
workspace root is normalized. This freezes configuration inventory, order,
source, location, redaction, adapter/transport identity, auth readiness,
billing, and the blocking error. JSONL must validate every event and contain
exactly one terminal event, last. Human rendering is not frozen yet: the two
products currently share canonical error data but no authoritative formatting
contract.

For package-aware rehearsal, repeat `--candidate LABEL RUNNER PATH` with the
exact already-installed executables. `RUNNER` is the required embedded identity
(`rust` or `bun`), so a Bun npm launcher can have a distinct surface label
without disguising its implementation:

```sh
python3 cli/conformance/runner/run.py --phase 7 \
  --candidate rust-installed rust /owned/rust/bin/prose \
  --candidate bun-installed bun /owned/bun/bin/prose \
  --candidate npm-launcher bun /owned/npm/bin/prose \
  --candidate-interpreter npm-launcher /owned/toolchain/bin/node
```

Candidate mode never builds. It allocates isolated workspaces for every label,
validates the embedded runner identity, and applies the same owned-process and
settled-output rules. It hashes the configured candidate leaf and resolved
target, copies the exact target bytes into a private read-only snapshot, and
executes that snapshot. Original and snapshot bytes are checked before and
after every case. This removes the ordinary source-path hash-to-exec race, but
it is not same-user kernel isolation: a process that can change permissions
could theoretically mutate and restore its own snapshot during execution.

An interpreter is optional and explicit; it exists for installed launchers such
as npm's `#!/usr/bin/env node` entry point, because the runner will not weaken
its empty executable-search path or discover ambient Node. The interpreter is a
separately trusted external tool. Its resolved installed path is executed after
exact leaf/target capture and pre-run checks, then checked again after the case.
It is not copied, because runtimes such as Homebrew Node resolve dynamic
libraries relative to their installed executable and copying one file does not
capture that closure. The report therefore fixes interpreter execution to
`resolved-original-path`, `ownedSnapshot: false`,
`relocatableClosureCaptured: false`, and
`execBoundaryToctouProtection: not-enforced`.

For the installed CommonJS npm launcher, Node evaluates the verified launcher
snapshot while using the resolved original launcher filename as its package
context. This preserves the launcher's sibling package resolution without
executing mutable launcher bytes. The installed package/resource closure is
still external and is not made immutable by this runner; release rehearsal must
independently reauthenticate the complete installed tree.

Add `--report-json NEW_PATH` to write one exclusive, canonical JSON evidence
file while preserving the normal text output and exit status. Existing output
is never overwritten. The closed report binds:

- the selected phase and sorted case IDs;
- candidate order, surface label, expected embedded Rust/Bun identity;
- the executable leaf and fully resolved target byte lengths and SHA-256s;
- explicit interpreter execution/custody mode and its non-enforced closure and
  exec-boundary limitations, when an interpreter is supplied;
- candidate-case and baseline differential validation counts; and
- bounded, path-normalized, credential-redacted failure records.

For a regular executable, leaf and target bind the same file bytes. For a
symlink launcher, the leaf digest binds the symlink target bytes returned by
the platform and the resolved-target digest independently binds executable file
bytes. Both are rechecked before and after every case. A mutation is retained
as `CANDIDATE_BYTES_CHANGED`; subsequent execution fails closed. This detects
custody loss but is not a filesystem immutability claim, so release rehearsal
must still supply owned package roots.

The report schema identifier is
`openprose.mechanical-conformance-report/1`. It deliberately contains no time,
host, workspace, transcript, model, cost, semantic-success, or release-ready
claim. Its fixed claims are limited to
`selected-provider-free-mechanical-corpus-only`, with detached-descendant
containment, semantic conformance, and release admission all false.

Run the direct installed-artifact signal oracle on supported POSIX hosts:

```sh
python3 cli/conformance/runner/signal_oracle.py
```

It starts the provider-free descendant fixture, polls its atomic identity file
without reading output pipes, sends SIGINT to the wrapper PID only, and requires
exit 24, exactly one JSON `CANCELLED` result, empty stderr, and disappearance of
the published child, grandchild, and fixture process group. Failure cleanup may
kill only the exact fixture group from that identity file. Native Windows is
explicitly skipped; no Windows cancellation inference is made here. The
controlled fixture remains inside its original group, so this oracle does not
close the `setsid()` escape described above.

The current runner/signal suite contains 39 tests, and the authoritative
provider-free run on 2026-08-31 was clean across 36 cases and both products.
Those results award only the fixed mechanical scope recorded in the report.
Current adapter verification also retains 16 shared-oracle tests, 17
cross-product adversary tests, 224 Rust workspace tests, and 396 Bun tests /
2,473 expects. Functional-alpha admission is an exact version allowlist; its
provider-key paths isolate Prime/OMP configuration, and every actual Prime
child receives the exact telemetry-off control. These product authorities do
not widen this runner's mechanical or containment claims.

The cost-acknowledged functional-alpha live lane is separate from this
provider-free runner. It retains a historical direct 8/8 Rust/Bun collection
and a historical evidence-v2 12/12 Rust/Bun/npm collection; neither was
relabeled. Current evidence v5 and matrix v4 close npm
launcher/meta/platform/native custody and bounded declared harness/runtime
custody. Exact source `31d81c55c8c90a7358b1cd8c5a0ccba631290a83` passed a
fresh 12-cell Darwin ARM64 collection, retained with its caveats under
[`live-alpha/evidence`](../live-alpha/evidence/31d81c55c8c90a7358b1cd8c5a0ccba631290a83/REPORT.md).
The 56 live-contract tests enforce these evidence boundaries and the closed
candidate-reported Prime failure projection without making failed runs
evidence. The matrix is candidate-reported, provider spend is unverified,
reliability is not measured, and semantic status is `not-applicable`. It does
not change this runner's nonsemantic, non-release claims, and no artifact from
this implementation has been published.


## Registry and service environment fixtures

The account and registry lanes use compiled test seams with temporary user
configuration, synthetic credentials, and exact ordered transport exchanges.
They never contact an account service or start a harness. Ordinary binaries
ignore the fixture environment variable. Build and run from the repository root:

```sh
(cd cli/rust && cargo build --locked --features test-seams --bin prose)
(cd cli/bun && bun run build:test)
python3 cli/conformance/runner/service_environment.py -- cli/rust/target/debug/prose
python3 cli/conformance/runner/service_environment.py -- cli/bun/dist/prose-test
python3 cli/conformance/runner/registry_service.py -- cli/rust/target/debug/prose
python3 cli/conformance/runner/registry_service.py -- cli/bun/dist/prose-test
```

Use the repository-pinned Bun 1.3.5 on PATH, including for child build scripts.
`run_local.py` includes `registry-service-rust` and `registry-service-bun` after
its test-seam builds. The registry oracle checks both canonical byte fixtures in
production and staging across publish, fetch, public listing, withdraw, and
artifact tampering, HTTP error classification, duplicate manifest keys and human
output. It asserts exact receipt results, exact posted bytes/hash,
selected fixed origin and credential isolation without retaining credentials.
The normative vectors live in `cli/shared/fixtures/registry/`; neither port
imports the other's implementation.

Local directory-manifest tests reject duplicate JSON keys; network artifacts
retain the protocol's parsed-object behavior and must also match canonical bytes
on fetch. Filesystem checks cover symlinks, unsafe paths, existing destinations,
and receipt-last Bun materialization. A Bun fetch may expose an incomplete
reserved directory during writes; no automatic resume or overwrite is promised.
Fixture success is not deployment, publication, native platform admission, or
live authorization evidence.
