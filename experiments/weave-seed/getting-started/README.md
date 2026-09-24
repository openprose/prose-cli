# Run the offline example

Start here to check the local loop without a login, credentials or network. This example uses deterministic subprocess fixtures and a synthetic agreement. It demonstrates observation, bounded repair, persistent state and reuse; it does not test OpenProse interpretation or model quality.

The synthetic agreement illustrates an outcome to achieve (a report matching the source) and a condition to maintain as the source changes. Ordinary code supplies both assessment and action here; these roles do not require a classifier or generator. Reuse in this example does not justify skipping a real program's required reviews, reports or other work for each invocation.

Use Bun 1.3.5 and a checkout containing this directory. From the repository root, create an example in a **new absolute directory whose parent already exists**:

```sh
bun --no-env-file experiments/weave-seed/getting-started/create.mjs /absolute/new-example
```

Replace `/absolute/new-example` with your chosen new directory. Setup refuses an existing directory or symlink. It creates private files, prints one JSON record containing the generated paths and executable argument arrays, and writes a README with exact commands in the example directory. It does not run the loop or contact a provider. Paths containing spaces are supported.

Run these commands from the same repository root, using the generated config path:

```sh
bun --no-env-file experiments/weave-seed/local/run.mjs check /absolute/new-example/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs status /absolute/new-example/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs step /absolute/new-example/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs step /absolute/new-example/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs serve /absolute/new-example/config.json --poll-ms 250 --max-steps 3
```

Offline check reports `configured` without invoking a capability, but explicitly does not verify a provider or assess semantics. It exits 2 with structured blockers if required local configuration is unavailable. Before the first step, status reports a null checkpoint. The first step copies `source.txt` to `report.txt` and reports `satisfied` with one cumulative attempt. The second reports `reused` while the evidence remains fresh. Bounded serve performs three observations, emits a JSON result for each, then a stop record with `step-limit`. Fresh unchanged satisfaction avoids both fixture processes; `calls.log` shows the calls that actually occurred. Evidence expires after 60 seconds, so a later step may reassess rather than reuse.

Edit `source.txt`, then repeat step or serve. The next step repairs the report and consumes another action from the same three-action budget. Remove `source.txt` to see `evidence-gap` without a capability call. Restoring a file does not reset the budget. The report, checkpoint and call log remain available after the process exits.

These commands are an experimental sidecar. They are not `prose init`, `prose serve`, a login flow or an HTTP server. No package has been published by this walkthrough. The generated config references this checkout and the Bun executable used during setup; keep both available. Moving the checkout requires reviewing the executable paths and capability identity. This slice does not install or provision anything.

For a stable installation of a private compiled bundle, use the [offline installation guide](../distribution/INSTALL.md) before generating a subject. Existing subjects retain their selected executable paths during side-by-side upgrades.

## Reading the output

Successful CLI output is newline-delimited JSON. Treat each line as a complete record. A step result includes `status`, cumulative `attempts` and an unresolved `pending` attempt, if any. Status exposes checkpoint and lock diagnostics; it is not an atomic authorization to act. Serve emits step records followed by its bounded stop record. Nonzero exit and stderr indicate a failed command; preserve that failure instead of assuming work completed.

| Observation | Next step |
|---|---|
| `satisfied` or `reused` | Inspect the selected artifacts and evidence scope; this fixture means exact file equality only. |
| `evidence-gap` | Restore or select the required readable input, then retry within the existing budget. |
| `unknown` | Supply sufficient evidence or review the assessor; unknown does not authorize another action. |
| Budget exhausted | Review prior attempts and the intended bound. Do not delete checkpoint state to disguise a reset. |
| Pending or recovery needed | Investigate the recorded attempt and actual effects; use the documented trusted settlement process before resuming. |
| Existing lock or busy service | Wait for the actual owner or perform trusted reconciliation. Do not automatically delete a lock based on a PID. |
| Configuration changed during serve | Stop, review the new policy, and restart explicitly. |

The coordinator is for cooperating processes in a trusted local filesystem. It has no distributed lease, hostile-filesystem isolation or immediate whole-process-tree cancellation. A timed-out actor may already have produced effects. Read [local ownership and recovery](../local/README.md) and the [host contract](../HOST.md) before using real actions.

## Next: your own provider and program

The [BYOK guide](BYOK.md) explains how to replace the fixture with an explicitly selected kernel, contract, Jev assessor and native actor. That transition can send data to providers and incur charges. The offline setup above does neither. It does not generate a live configuration automatically.

## Native Rust coordinator

The [Rust local coordinator](../rust-local/README.md) uses the same configuration and checkpoint format for native `step`, `status` and bounded `serve` on its documented Unix domain. When working from a source checkout, build it with Cargo and cached dependencies for an offline build. A private compiled review bundle already includes `bin/weave-rust`; use that executable with the same commands and generated config. The generated fixture and provider capabilities still use Bun even when Rust owns the loop. Native Rust also provides the same offline `check` status/error schema. Its reported runtime version is the Rust package version, not the compiler version. Do not infer cross-platform or provider qualification from the shared protocol.

## Verification

Run the complete offline journey with:

```sh
bun --no-env-file test experiments/weave-seed/getting-started/walkthrough.test.mjs
bun --no-env-file test experiments/weave-seed/getting-started/configure.test.mjs
```

The tests launch the actual setup and coordinator subprocesses with empty environments, private temporary directories and no provider capabilities. They check create, read-only status, first repair, fresh reuse, bounded serve, changed input, missing input, cumulative attempts, paths containing spaces, and refusal to overwrite existing paths. A third test copies the required source package into a fresh location and verifies its generated capabilities resolve there. Bun 1.3.5 passed all three test groups. This is local source-checkout evidence, not a public installed-package or cross-platform qualification claim.

The BYOK configuration tests use a fake nonrunning CLI, reviewed synthetic source files and empty environments. Five groups verify exact generated bindings, offline adapter validation, CLI digest checks, unknown-field rejection, cleanup/refusal, missing credential-name diagnostics and private actionable error stages. They make no provider calls.

The generated BYOK configuration can also be exercised entirely offline:

```sh
bun --no-env-file test experiments/weave-seed/getting-started/generated-loop.test.mjs
```

That test runs `configure.mjs`, then explicitly substitutes a test assessor shim that imports the actual Jev adapter and injects an in-memory HTTP response. It uses the actual native actor adapter with a fake executable that implements the native readiness/completion protocol. The shim and changed outer config are selected evidence. Children inherit no ambient environment; two fixed fake credential values satisfy the generated variable-name selections. No HTTP request or real model call occurs.

The real coordinator subprocesses perform check, status, repair, reuse, bounded serve after a state change, bound-question invalidation, invalid-question rejection, and restart after a fake action writes an effect then fails. That last step returns `action-outcome-unknown` with pending intent; a zero coordinator exit does not turn that status into success. Restart returns `recovery-needed` without replaying the action. These are generated-config linkage and failure-ordering tests, not provider, native-model or semantic qualification.

To exercise the same generated checkpoint across both runtimes, supply an already-built native Rust coordinator:

```sh
WEAVE_RUST_LOCAL=/absolute/path/to/weave-rust-local \
  bun --no-env-file test experiments/weave-seed/getting-started/generated-loop.test.mjs
```

Both the Bun-only and Bun/Rust variants passed locally. The Rust variant still selects Bun adapter processes explicitly; it does not claim a Bun-free provider stack.

## Generate an explicit `prose cli weave` host binding

For a CLI build containing the experimental `cli weave` bridge, generate its host binding without manually hashing a binary. This helper works with an existing synthetic or BYOK config and an explicitly selected compiled sidecar (`weave-bun` or `weave-rust`). It does not modify that config, inspect credentials, run either executable, call a provider, or claim readiness. No CLI version, public installation or cross-platform compatibility is inferred from an executable file.

```sh
bun --no-env-file experiments/weave-seed/getting-started/host-binding.mjs \
  --host /absolute/private-installation/payload/bin/weave-rust \
  --config /absolute/subject/config.json \
  --output /absolute/subject/host-binding.json \
  --environment-keys '[]' \
  --timeout-ms 900000 \
  --max-output-bytes 1048576 \
  --prose /absolute/selected-prose-cli
```

All seven options are required. Their order may vary; duplicates, unknown options, equals forms and noncanonical decimal numbers are rejected. The output's parent must exist and the output file must be new; existing files, directories and symlinks are never replaced. Setup writes a private mode-0600 file with exactly the bridge binding schema and a SHA-256 of the complete selected host executable. The host must be an executable regular file of at most 512 MiB. Executable symlinks resolve to their canonical target. The supplied absolute config path is preserved, because changing a symlink-parent spelling can change config-relative source resolution. Setup checks that it names an existing regular file but does not parse or validate the coordinator config.

The result is one JSON record with `status: "binding-created-not-executed"`, `hostExecuted: false`, `cliProbed: false`, and `providerVerified: false`. Its `commands` are authoritative argument arrays, and `shellCommands` contains individually quoted, copyable POSIX-shell equivalents for `check`, `status`, `step`, and `serve`. Review them before running anything; serve's generated example uses a 1000 ms poll and **one** maximum step. For example, after reviewing the generated paths:

```sh
/absolute/selected-prose-cli cli weave \
  --host-binding /absolute/subject/host-binding.json \
  check /absolute/subject/config.json
```

`--environment-keys` is an explicit JSON array of unique environment-variable **names**, at most 128 names of at most 128 ASCII identifier characters each. `[]` passes an empty environment and works for the offline fixture's absolute executables. A live config may require an explicitly reviewed list such as `["OPENAI_API_KEY"]`, or `PATH` if its selected native harness needs it. The helper neither reads those variables nor infers additional names from the config. The bridge copies only present selected values when a command actually runs; the coordinator's separate environment policy still applies. Selecting names does not verify that credentials exist or work.

The bridge timeout must be 1..86400000 ms and the combined child stdout/stderr budget 1..16777216 bytes. These are whole-command limits, including a complete serve invocation; they are independent of provider/action deadlines and the cumulative action budget. Expiry or interruption may leave pending effects or conservative locks. Use the [explicit recovery guide](../local/RECOVERY.md); generating a binding does not settle effects, unlock state or reset attempts.

The digest proves which selected bytes were recorded, not who published them. Trusted parent-path resolution, shebang interpreters and concurrent filesystem replacement remain outside this helper's attestation. The bridge rechecks the host digest before launch. A changed host requires an explicitly reviewed **new** binding file; setup does not update an old binding or migrate a configuration/checkpoint. A failed write can leave an unaccepted partial output: inspect it and choose a fresh path rather than running it. File fsync alone is not a universal power-loss guarantee.

This helper is standalone: it can be copied to another directory and run with explicit absolute inputs, without importing the source checkout. Run its nine offline test groups with:

```sh
bun --no-env-file test experiments/weave-seed/getting-started/host-binding.test.mjs
```

Tests cover exact hashing and private output, spaces and symlink resolution, config preservation, overwrite refusal, strict bounds/names/arguments, oversized and FIFO inputs, and the fully copied helper with an ambient secret canary. The selected fake host and CLI deliberately cannot execute; successful setup is evidence that the helper did not probe them. No provider is called.
