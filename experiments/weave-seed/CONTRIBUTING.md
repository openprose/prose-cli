# Contributing to the Weave seed

This is an experimental development surface. Read the [overview](README.md), [specification](SPEC.md), and [readiness assessment](../../docs/weave-v1-readiness.md) before changing it. Existing CLI development remains governed by [CLI instructions](../../cli/AGENTS.md) and [CLI contribution guidance](../../cli/CONTRIBUTING.md). A seed change does not authorize a product command, release, repository migration, or kernel change.

## Human and agent workflow

1. Identify the owning layer. Kernel and contract meaning belongs in the selected Markdown definitions; evidence selection, storage, permissions, scheduling, and provider transport belong in the host; step ordering belongs in this experiment.
2. Read the actual assigned task and applicable local instructions. In a coordinated worktree, obtain an exact path lease from its lead before edits. Preserve other contributors' work and leave Git operations to the assigned owner.
3. State the observable behavior in the shared fixture or specification before implementation. Implement the same contract independently in Rust and JavaScript. Neither implementation is the oracle for the other.
4. Use synthetic local evidence, deterministic callbacks, fixed clocks and attempt identities, bounded resources, and fresh temporary state. Ordinary validation must make no network or provider calls.
5. Run the affected local checks, retain failures, and report the source revision, runtime versions, exact commands, results, and limitations. A passing fake evaluator does not establish model accuracy or authorization.
6. Leave a reviewable diff and a short handoff describing unfinished obligations. Do not silently broaden scope, loosen a failed gate, publish, or migrate origins.

## Verification scope

The [Python quick start](README.md#first-local-run) verifies the existing local reference. Seed parity commands and measured runtime versions are recorded in the [readiness assessment](../../docs/weave-v1-readiness.md). Test only installed, identified runtimes; report unavailable tools instead of substituting a runtime and calling it parity.

New fixtures should include negative controls: missing or stale evidence, changed bindings, unresolved effects, exhausted attempts, action failure, and an executor that returns without satisfying the obligation. Assess the complete invocation, including reporting and required operations. Reused satisfaction of one maintained artifact is not proof that the whole invocation is fulfilled.

Live model testing requires a separate explicit protocol with provider-specific attempt, time, concurrency, and spending limits, an independent evaluation method, and retained failure evidence. This guide and the examples do not authorize it.

## Documentation and evidence

Write clear Standard Technical English. Keep source observations, implementation claims, and fulfillment judgments distinct. Record which operations were observed rather than claiming an artifact was read because it exists. Use portable relative links and exact revisions for reproducibility. Do not commit credentials, private paths, raw customer data, or unreviewed provider transcripts.

Use the [feedback template](FEEDBACK.md) to report a defect with the smallest synthetic reproducer. Feedback remains a manual choice; the seed has no automatic telemetry or issue submission workflow.
