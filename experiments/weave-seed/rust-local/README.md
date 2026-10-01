# Native Rust local coordinator

This unpublished Unix crate provides native file observation, durable Rust loop execution and a bounded local polling service. It embeds the sibling `rust-binding` and `rust-host` libraries. The runtime does not require Bun. Evaluator and executor executables are explicit caller-selected capabilities; choosing a Bun-based adapter still requires Bun for that capability. No provider, account, hosted service or language interpreter is selected implicitly.

Build and run from the repository root:

```sh
cargo build --offline --locked --manifest-path experiments/weave-seed/rust-local/Cargo.toml
experiments/weave-seed/rust-local/target/debug/weave-rust-local --help
experiments/weave-seed/rust-local/target/debug/weave-rust-local check /absolute/config.json
experiments/weave-seed/rust-local/target/debug/weave-rust-local step /absolute/config.json
experiments/weave-seed/rust-local/target/debug/weave-rust-local status /absolute/config.json
experiments/weave-seed/rust-local/target/debug/weave-rust-local serve /absolute/config.json --poll-ms 1000 --max-steps 60
```

If dependencies are not cached, explicitly run `cargo fetch --locked --manifest-path experiments/weave-seed/rust-local/Cargo.toml` first. The executable is built from this checkout; this is not a packaged or installed public Prose command. Native `check` uses the same offline diagnostic schema as the Bun local diagnostic.

## Shared configuration and protocol

Use the [Bun local configuration](../local/README.md) and [integration contract](../integration/README.md) unchanged. Root and checkpointDirectory resolve from the config file's directory. Sources resolve from root. Commands must be arrays with absolute executables, literal arguments and no NUL bytes. `environmentKeys` is an explicit ordered selection of existing parent variables, with no ambient environment passed to children. Literal `environment` values are rejected; credentials do not belong in this JSON or observation files.

The native implementation matches the current ordered policy hash: `version,assessor,actor,environment,timeoutMs,maxOutputBytes`. Environment preserves selected key order and values, including names that collide with JavaScript object properties. Policy changes invalidate prior satisfaction. The shared `rust-binding` v1 digest and payload are byte-compatible with the Bun binding on the documented UTF-8 Unix input domain. Paths, capabilityVersion and environment values bind policy; arbitrary executable contents or files mentioned by argv are not automatically discovered. Explicitly bind policy/configuration files as sources and advance capabilityVersion when transitive capability code changes.

Each capability receives one newline-terminated JSON object:

```json
{"schema":"openprose.weave-input/1","evidence":{"identity":"...","payload":"...","observedAt":10,"validUntil":60010,"gap":false},"attempt":null}
```

Assessment requires exactly a literal `{"judgment":"satisfied"}`, `{"judgment":"work-needed"}` or `{"judgment":"unknown"}` object, with JSON whitespace allowed. Extra fields, escapes replacing literal protocol tokens, duplicate keys, malformed JSON, invalid UTF-8 and nonzero exit fail. Action receives an opaque nonempty attempt ID; its zero exit only means completed action transport. The core reobserves and reassesses before reporting satisfaction. Failed action processes leave pending intent; they do not authorize replay.

`step` emits one `{status,attempts,pending}` JSON record. `status` emits `{serviceOwned,checkpointLocked,checkpoint}` without creating directories. Missing checkpoint is null; corrupted checkpoint is an error. The diagnostic checkpoint omits the on-disk schema field, matching Bun. `serve` emits a record per completed step and a final `{stopped,steps}` record. Diagnostics use stderr and errors exit nonzero. `--help` succeeds without configuration or filesystem writes.

## Offline diagnostic

`check CONFIG` performs local inspection only and emits `openprose.weave-check/1`. It validates configuration and selected environment names, observes required files, checks that each executable is a regular file with X_OK permission, and reports checkpoint presence/pending/corruption and existing locks. It never launches a capability, opens a network connection, creates state, takes a lock or deletes one. `configured` exits 0; `blocked` exits 2 with structured JSON and no diagnostic stderr. Errors include the same stable configuration, environment, source-gap, executable, recovery, checkpoint and ownership codes as Bun.

`providerVerified` and `semanticAssessment` are always false. Availability now is neither future execution authorization nor a provider credential/balance check. Environment values, command arguments, source contents and policy hashes are omitted. Missing names are reported without exposing values. `runtime` is `{ "name": "rust", "version": "0.0.0" }`: version is this experimental crate's Cargo package version, not an asserted compiler or language version. Status/check observations are non-atomic and can become stale immediately.

## Explicit settlement after trusted investigation

```sh
experiments/weave-seed/rust-local/target/debug/weave-rust-local settle /absolute/config.json --binding RECORDED_BINDING --attempt RECORDED_ATTEMPT --outcome completed --receipt 'reviewed receipt reference'
```

The fixed argument order is required. Use `completed` or `not-applied` only after investigating the recorded attempt; see the shared [recovery procedure](../local/RECOVERY.md). Settlement uses the exact pending binding and attempt, not a newly computed source binding. It clears pending, records the explicit outcome/receipt, invalidates satisfaction and expiry, and preserves cumulative attempts. It does not reobserve, launch providers, infer completion, undo effects, reset budgets or remove existing locks. Receipt text should be a reviewed reference, not a secret.

The locator configuration needs only schema 1 and a nonblank checkpointDirectory; absent provider environment, missing source files and invalid current capability settings do not prevent recovery. Both the service and host locks still apply. Wrong identity, no pending attempt, unsupported outcome, blank receipt, corrupt checkpoint or existing ownership refuses settlement. The CLI emits `{status:"settled",attempts,pending:null}`; the SDK function returns the settled core checkpoint. A later explicit step must reassess. There is no unlock command.

## Native embedding

The library exports `check_config(path)`, `step_config(path)`, `status_config(path)`, `settle_config(path, binding, attempt, outcome, receipt)`, `acquire_owner(path)` and `serve_config(path, poll_ms, max_steps, cancelled, on_step)`. `StepResult` exposes the core checkpoint, status and `projection()`. `Owner::step()` performs one step while retaining the service lock; `step_with_cancel(&AtomicBool)` enables cooperative subprocess cancellation. `release()` explicitly reports release failures; dropping an owner attempts cleanup only when directory identity still matches. `ServeResult` has steps, stopped and the optional last result. The trusted callback receives `(&StepResult, step_number)` and may return an error.

Serve accepts poll intervals 1–3,600,000 milliseconds and 1–1,000,000 steps. Every poll observes selected files. Identical fresh satisfied evidence reuses the checkpoint with no capability calls; expiry or changed bound evidence reassesses. Missing evidence stays a gap. Pending intent stops serve immediately. Attempts remain cumulative across processes and restarts. The service retains only the last step, not an unbounded history.

## Ownership and failure boundaries

The exact existing `checkpoint.json`, `lock`, and `service.lock` names interoperate with Bun. A mkdir service lock remains held throughout serving, including idle time; the underlying host lock protects each durable step. Locks are advisory between cooperating controllers, not a distributed lease. Direct calls to the older integration runner or raw host bypass the service lock. Existing locks are not automatically removed or stolen. Release checks directory device/inode and preserves a replacement lock.

Configuration bytes are frozen for an owner's lifetime. A change is rejected before the next step. This does not prevent an adversarial change between validation and use. Observation remains a sequential trusted-filesystem snapshot; it is not atomic or a confinement boundary. The Rust host retains its uncertain-save lock and corrupt state is never reset. Durability limitations, including newly created ancestor directories and operator reconciliation, remain those in [HOST.md](../HOST.md).

Children run without a shell or PTY, with cleared environment, nonblocking concurrent pipe I/O, explicit timeout, and bounded capture for each of stdout and stderr. No reader threads remain detached. On timeout, capture error, nonzero exit or cancellation, the native adapter attempts SIGKILL of the child's Unix process group and reaps the direct child. A descendant that deliberately escapes the group, an already completed external effect, or a stalled kernel operation is not contained by this mechanism. Successful child completion does not prove no background descendants exist.

SIGINT/SIGTERM in the serve CLI request cancellation, interrupt idle waits and are checked during subprocess execution. An executor interrupted after durable intent remains pending, with `action-outcome-unknown`, and must be reconciled before another action. Cancellation during assessment may return a nonzero error before a step completes; it cannot be reported as a successful assessment. Hard termination may leave both locks. The library does not install signal handlers; callers supply their own AtomicBool. These are stronger cooperative process controls than Bun's synchronous direct-child timeout, so exact signal/process-tree behavior is not claimed identical across runtimes.

The configuration must be a regular file with a 1 MiB bound; nonblocking open and metadata checks reject FIFOs and oversized files before reading. UTF-8 decoding is fatal. Safe-integer configured byte/time bounds match the integration contract, but enormous caller-selected limits can still exhaust resources; select practical limits. Process timeouts do not impose a wall deadline on file observation or arbitrary trusted embedding callbacks. No provider spend enforcement, hostile-process sandbox, continuous daemon supervision, background installation or hosted transfer is provided. Unix and UTF-8 paths are required.

## Provider-free validation

```sh
cargo test --offline --locked --manifest-path experiments/weave-seed/rust-local/Cargo.toml
bun --no-env-file experiments/weave-seed/rust-local/conformance.mjs
bun --no-env-file experiments/weave-seed/rust-local/check-parity.mjs
bun --no-env-file experiments/weave-seed/integration/local-parity.test.mjs /absolute/repository/experiments/weave-seed/rust-local/target/debug/weave-rust-local
```

Both parity scripts accept an optional native binary path as their first argument and an optional fixture executable path as the second. By default the fixture is selected beside the chosen native binary, so an external build directory does not silently fall back to checkout build artifacts. Both scripts passed against a freshly compiled temporary target directory.

The extra `weave-local-test-fixture` binary is a synthetic test capability, not an OpenProse evaluator. It compares source.txt/report.txt and can inject failures; it is not part of the production authority surface. Native tests invoke it with an empty or explicitly selected environment and temporary files, without Bun, credentials, network or models.

Validation on macOS arm64 using rustc/cargo 1.98.1 and Bun 1.3.5: 24 native tests; 18 direct interoperability checks; 18 read-only diagnostic comparisons; and the shared integration suite's 211 alternating-runtime steps, including 100 actual repairs and 100 cross-runtime reuses. Checks cover changed/expired evidence, pending after effect failure, cumulative attempts, policy invalidation, full checkpoint projection, service exclusion, replacement locks, invalid/corrupt state, environment selection, process timeout/output limits, inherited pipes, callbacks, CLI help and actual SIGTERM during idle and actor execution. Initial test-only 100 ms startup/TTL assumptions were too short under concurrent scheduling; the final tests use wider bounds. Two signal tests also now wait for a fully parsed actor PID or a satisfied checkpoint with the host lock released, replacing timing-based assumptions. No production semantics were weakened. Rustfmt is unavailable in the installed toolchain; formatting validation is not claimed. These results establish bounded local mechanics, not model-backed agreement reliability or installed-product qualification.

## Private SDK composition without checkout fallback

The Rust packages are private, unpublished path packages, not registry releases. Distribute the sibling directories `rust/`, `rust-binding/`, `rust-host/` and `rust-local/` together for the complete native SDK. The host now declares an ordinary path-and-version dependency on `openprose-weave-experimental` and reexports it as `core`; it no longer includes a source file from outside its package. Host and direct core consumers therefore use the same Rust types. The local crate declares the host and binding as ordinary versioned path dependencies as well. All four package versions remain `0.0.0` with publishing disabled.

```sh
python3 experiments/weave-seed/rust-local/consumer-check.py --cargo /absolute/path/to/cargo
```

This offline qualification copies all four package directories into fresh temporary sibling directories, excludes build artifacts, writes an independent consumer, and checks Cargo metadata to reject any path dependency outside that copy. It builds with a fresh target directory and a generated consumer lockfile, using only cached registry dependencies. The consumer imports core types directly and through the host reexport, performs native file observation and a durable repair, reconstructs the host and reuses the checkpoint without another action, then calls native local readiness and status. It launches no capability or model. Temporary files are removed after completion. No Bun executable or original source path is available through this consumer's dependency graph.

This proves private library consumption of the complete native stack, not publication to crates.io, a supported version matrix, or arbitrary independent package relocation. Sibling path dependencies still require that declared layout. Some repository conformance tests reference shared fixtures outside individual crate directories; the copied consumer does not run dependency test suites or claim their standalone packaging. The production dependency graph itself no longer relies on a host source include outside its crate.
