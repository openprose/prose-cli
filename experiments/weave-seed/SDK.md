# Embedding the weave

The weave is a small execution library. A host supplies observation, assessment, action, durable checkpoint storage, a clock and attempt identifiers. The library decides whether to reuse a judgment, assess evidence, attempt work, or stop for recovery. The selected OpenProse kernel and contracts define the obligations; the library does not parse Markdown or discover dependencies.

Contracts can define outcomes to achieve and conditions to maintain. Assessor and actor name roles, not provider types: ordinary code, the same agent or other authorized participants may supply either capability, subject to any independence required by the selected agreement. A classifier and a generator are possible implementations. Embedding this optional loop does not replace the selected agreement's required reviews, reports, constraints or other invocation obligations.

Both implementations are unpublished source packages at version `0.0.0`. They are suitable for local experiments, not a released v1 compatibility commitment. No account, network connection or provider key is required to embed the core.

## Bun

The seed has a private `package.json` with these exports:

| Import | Capability |
| --- | --- |
| `@openprose/weave-experimental` | Synchronous `reconcile`, `emptyCheckpoint`, and explicit `settlePending`. |
| `@openprose/weave-experimental/host` | Trusted local checkpoint persistence. |
| `@openprose/weave-experimental/binding` | Bounded selected-file observation and content identity. |
| `@openprose/weave-experimental/process` | Explicit subprocess assessment and action capabilities. |
| `@openprose/weave-experimental/local` | `stepConfig`, `statusConfig`, `settleConfig`, and bounded `serveConfig`. |

Use a local path dependency on this directory or copy its source package into a private consumer project. There is no registry installation command yet. The [small callback example](examples/local.mjs) demonstrates the core API; replace its relative import with the package name when using a dependency. The [local coordinator](local/README.md) shows durable configuration-based execution.

Callbacks are synchronous. An asynchronous provider runs in a child process through the process capability; passing a Promise to the core is rejected. This keeps checkpoint and effect ordering explicit, but a synchronous child blocks the Bun coordinator until it returns or times out.

## Rust

The core is a dependency-free Cargo library:

```toml
[dependencies]
openprose-weave-experimental = { path = "../weave-seed/rust" }
```

Import it with `use openprose_weave_experimental as weave;`. Implement its `Host` trait and call `weave::reconcile`. The [Rust callback example](examples/local.rs) shows the same repair-and-reuse sequence as Bun. Its standalone source include can be replaced with the crate import when embedding.

The [Rust checkpoint host](rust-host/README.md) and [native file observer](rust-binding/README.md) are separate experimental crates. This separation allows an embedding application to use another storage or observation mechanism without adding filesystem behavior to the core.

## What a host must provide

A binding identifies the selected agreement, evidence selector and assessment/action policy. Evidence contains an identity, payload, observation time, validity limit and an explicit gap flag. The process integration uses SHA-256 content identities; the generic core treats identities as host-supplied values.

The host must supply the actual effective agreement and evidence needed to judge it. It must also grant and enforce allowed effects. Neither a file hash nor a classifier's positive answer proves complete contract compliance. Missing evidence produces a gap or unknown result, not permission to act.

Attempts are cumulative for a checkpoint. Before action, the host durably saves a pending attempt. After normal return, the core observes and assesses again. An uncertain action remains pending and is not replayed on restart. Explicit settlement requires investigation and a receipt; it does not restore the attempt budget or reverse an external effect. The local sidecars expose the same operation through the [recovery workflow](local/RECOVERY.md).

See [SPEC](SPEC.md) and [HOST](HOST.md) for exact lifecycle and persistence rules. Local advisory locks are not distributed ownership. Local sequential file reads are not an atomic world snapshot. Capability code and its dependencies must remain fixed or receive a new `capabilityVersion`; referenced policy files must be selected evidence so their content changes invalidate reuse.

## Check an independent consumer

With Bun and Cargo installed and required Rust dependencies already cached:

```sh
WEAVE_CARGO="$(command -v cargo)" bun --no-env-file experiments/weave-seed/integration/sdk.test.mjs
```

This test copies the selected package files into new temporary consumer directories. The JavaScript consumer imports by package name, including the exported host helpers. The Rust consumer uses a Cargo path dependency and builds offline. Both assert one action followed by reuse. Neither can resolve its library back to this checkout. This is source-package consumer qualification; signed binaries, published archives and clean-machine installation remain separate release checks.
