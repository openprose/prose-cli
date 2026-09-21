# Weave development seed

Weave is an unpublished experiment in coordinating assessment and action for outcomes to achieve and conditions to maintain. This directory contains independent Rust and JavaScript loop implementations for comparison. It is a locally consumable source SDK and sidecar, not a published package or a `prose weave` command. Start with the provider-free seed examples below; see [candidate readiness](../../docs/weave-v1-readiness.md) before making product or release claims.

The selected OpenProse kernel and adopted contracts remain authoritative. They define the entire invocation's obligations, including required reports and steps. The loop receives an explicit binding and evidence through caller-supplied capabilities. It neither parses prose nor discovers an agreement. An assessor judges evidence against the resolved agreement; an actor attempts permitted work. These are provider-neutral roles: the same agent, ordinary code or other authorized participants may supply either role, subject to any independence required by the selected agreement. A classifier and a generator are possible implementations, not requirements. The runtime controls their sequence and checks for changed or expired evidence.

## Start here

For a private compiled bundle, first follow [offline installation](distribution/INSTALL.md). Its receipt exposes the installed setup helpers. The [host-binding helper](getting-started/README.md#generate-an-explicit-prose-cli-weave-host-binding) connects an explicitly selected Prose CLI to either coordinator and prints both argument arrays and copyable POSIX commands. The experimental command is `prose cli weave`; top-level language commands are unchanged.

1. [Create and run the offline example](getting-started/README.md). One setup command creates a private example directory and prints the exact commands to inspect, repair and watch it. No credentials or network are used.
2. [Configure your own program and providers](getting-started/BYOK.md). Keep the selected kernel, contracts and evidence explicit. The local profile uses a Jev assessor and the existing Agents SDK action route with the caller's keys; it requires no OpenProse account.
3. [Embed the SDK](SDK.md) in a Bun or Rust application. The core is independent of provider, storage and command-line choices.

The [Bun coordinator](local/README.md) and [native Rust coordinator](rust-local/README.md) expose bounded local steps, persisted status and serving. Ordinary code observes selected files; changed content or expired acceptance triggers assessment. Work is attempted only after an actionable assessment, then the evidence is read and assessed again. Unchanged fresh acceptance avoids both provider calls.

These are unpublished sidecars and source packages. Existing top-level Prose language requests remain unchanged. Login, hosted deployment and publication are planned separately and are not needed for local execution. The initial actor profile is Agents SDK with an OpenAI API key; other providers have not been qualified through this adapter.

## First local run

From this repository's root, with Bun 1.3.5 and Rust 1.98.1 on `PATH`:

```sh
bun experiments/weave-seed/examples/local.mjs
WEAVE_EXAMPLE_DIR=$(mktemp -d)
rustc --edition=2021 experiments/weave-seed/examples/local.rs -o "$WEAVE_EXAMPLE_DIR/local"
"$WEAVE_EXAMPLE_DIR/local"
```

Both examples assert one repair followed by reuse and print `satisfied`, `reused`, and one action. Their storage is in memory; they demonstrate callback sequencing, not durable recovery. They use no dependencies, credentials, network, or models. The temporary Rust binary remains in the newly created directory for inspection.

For the Python reference's SQLite and persisted-checkpoint demonstration, with Python 3 available:

```sh
python3 experiments/weave/demo.py
python3 -m unittest discover -s experiments/weave -p 'test_*.py' -q
```

The demonstration creates a temporary SQLite database and checkpoint, then removes them on exit. It needs no credentials, network, or model. Expect five events with statuses `satisfied`, `reused`, `satisfied`, `reused`, and `unknown`. The totals are four assessments and one action. The approval-revoked event abstains. The current reference suite contains 49 tests. These deterministic checks establish local mechanics, not semantic classifier reliability.

Run both seeds against their shared lifecycle fixtures:

```sh
bun experiments/weave-seed/bun/conformance.mjs
WEAVE_TEST_DIR=$(mktemp -d)
rustc --edition=2021 --test experiments/weave-seed/rust/lib.rs -o "$WEAVE_TEST_DIR/tests"
"$WEAVE_TEST_DIR/tests"
```

The measured results are 25 shared lifecycle cases in each implementation, with additional save-failure and settlement checks. Rust reports four test functions containing those checks and a 10,000-step sequential stress run. Bun also runs 10,000 sequential transitions. Both check cumulative actions, reuse, changing bindings, expiry, and budget exhaustion. Bun also checks mutable observation snapshots and rejection of asynchronous callback results. See [the validation record](../../docs/weave-v1-readiness.md#retained-qualification-evidence) for scope and versions.

Read the [walkthrough](examples/README.md) to understand the evidence and action boundary. Then read the [seed specification](SPEC.md) for the Rust and JavaScript capability interfaces and parity fixtures. The [Python reference](../weave/README.md) also includes a local checkpoint host and file/SQLite observers; their presence does not establish equivalent host services in the seeds.

## Supply the host explicitly

A host must resolve the contract, select evidence, enforce permissions, serialize work, persist checkpoints, supply time and attempt identities, and bound attempts and provider costs. Its binding must identify the effective agreement, selector, assessment policy, and permissions. A changed agreement or relevant policy must invalidate reuse.

Evidence must cover every obligation being assessed. If an invocation owes a new report for each batch, unchanged maintained state alone cannot justify skipping that report. A digest identifies selected content; it does not prove source authority, completeness, or freshness. Evidence gaps and `unknown` assessments do not authorize action.

One step can attempt at most one action. The pending attempt must be saved before the effect. Normal actor return is followed by fresh observation and assessment; it is not fulfillment. An interrupted or uncertain action requires [explicit recovery](local/RECOVERY.md) before replay. Attempt limits are cumulative for the checkpoint, and settlement does not replenish them. External effects are not transactional with the checkpoint, and no exactly-once guarantee is made.

## Build a private review bundle

[Bundle tooling](distribution/README.md) builds standalone `weave-bun` and `weave-rust` sidecars from copied source, inventories their bytes, and tests both after relocation. It performs no publication or installation. Current artifact qualification is macOS arm64; provider adapters remain explicit dependencies.

## Develop or report a problem

Use [Contributing](CONTRIBUTING.md) for human and agent changes, and [Feedback](FEEDBACK.md) for a reproducible, manually reviewed issue report. No example collects credentials, uploads evidence, or submits feedback automatically.

The kernel currently has its own source repository. A future kernel-origin migration to `openprose/prose` is planned, but this seed does not perform that migration, change live URLs, or upgrade consumer pins. Use the kernel source and exact package identity already selected by your host.

## Local persistence and recovery

Both implementations now have experimental local hosts under the [shared host contract](HOST.md). They save checkpoints atomically and serialize access in a trusted directory. A lock left by abrupt termination or uncertain persistence requires explicit reconciliation; it is never automatically stolen.

Run the Bun host checks with `bun --no-env-file experiments/weave-seed/bun/host.test.mjs`. See the [Rust host guide](rust-host/README.md) for its standalone Cargo crate and tests. The [file/process integration](integration/README.md) includes a Bun subprocess bridge and cross-runtime checkpoint tests; the [native Rust coordinator](rust-local/README.md) supplies the corresponding native process route. It does not yet connect a shipped CLI command to arbitrary kernel-backed programs.

These additions test local restart and failure behavior. Power-loss durability, distributed storage, hostile filesystem behavior and model-based fulfillment remain separate qualification gates.

For a file-backed walkthrough with inspectable retained state, use the [Bun persistent example](bun/HOST-EXAMPLE.md) or the [Rust persistent example](rust-host/README.md). Both recreate the host between events and demonstrate two repairs followed by abstention on corrupt and missing evidence. They use deterministic rules, not a generative provider.
