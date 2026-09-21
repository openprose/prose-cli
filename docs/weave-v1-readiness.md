# Weave candidate readiness

Status: unpublished local candidate, September 18, 2026. The local implementation is usable for bounded review. It is **not qualified for v1 promotion or public distribution**. Login, hosted transfer and backend work remain deferred.

The conceptual model is assessment and action over contracts defining outcomes to achieve and conditions to maintain. Assessor and actor are provider-neutral roles, potentially supplied by the same agent, ordinary code or other authorized participants, subject to the selected agreement's independence requirements. The implemented adapters and their qualification limits remain narrower than that model. Required reviews, reports, constraints and other invocation obligations still apply when maintained artifacts are already correct. This framing does not qualify a new kernel or change the retained evidence below.

## Current implementation

The [SDK](../experiments/weave-seed/SDK.md) provides independent Bun and Rust loop implementations. Native coordinators provide `check`, `status`, bounded `step` and `serve`, and explicit `settle`. Both observe selected files, preserve cumulative attempts and pending effects, and share checkpoint and binding formats. The Rust coordinator no longer delegates observation to Bun; the selected Jev and native action adapters still require Bun.

The reserved [CLI bridge](../cli/protocol/decisions/imp026-weave-host.md) is implemented in both products:

```text
prose cli weave --host-binding ABS check CONFIG
```

Its reviewed binding selects a coordinator executable digest, environment names, timeout and output limit. The bridge carries bounded process I/O; the coordinator owns loop and recovery semantics. Top-level requests such as `prose init` remain framework-owned and opaque. The bridge does not parse Markdown, resolve adoptions or introduce a framework initialization command.

Kernel and adopted contract semantics remain authoritative. Explicit file identities bind the assessment inputs; their hashes establish byte identity, not authority, truth or fulfillment. No kernel-origin migration or live URL change is part of this candidate.

## Developer and operator path

1. Follow the [local walkthrough](../experiments/weave-seed/getting-started/README.md) to create a synthetic example in a fresh directory and run offline check, repair and reuse.
2. Use the [private installer](../experiments/weave-seed/distribution/INSTALL.md) with a trusted manifest digest. It verifies the complete inventory, writes a fresh private installation and exposes installed helper paths. It does not run executables, alter PATH or migrate subject state. Upgrades are side by side.
3. Generate an explicit host binding with the installed helper. It hashes the selected coordinator and prints argument arrays and quoted POSIX commands. The native Prose CLI remains a separately selected installation.
4. For provider work, use the [BYOK guide](../experiments/weave-seed/getting-started/BYOK.md). Setup materializes reviewed configuration without calls or credential values; configuration and question bytes are selected evidence.
5. Investigate uncertain effects before using [recovery](../experiments/weave-seed/local/RECOVERY.md). Settlement requires the exact pending identity, outcome and receipt. It preserves attempts, expires prior satisfaction and does not invoke a provider. There is no automatic unlock or replay.

The [native actor](../experiments/weave-seed/integration/native-actor/README.md) currently supports the explicit Agents SDK/OpenAI-key profile. OpenRouter and other action profiles are not qualified here. The actor admits only a digest-pinned fixed-image CLI with test seams disabled and one payload at `payload/kernel.md`. It checks the selected kernel against that image before effects. Moving published-on-run selection is rejected. The [staging guide](../experiments/weave-seed/getting-started/FIXED-IMAGE.md) uses the official image tooling; native building remains an explicit step. There is no runtime kernel override or arbitrary multi-payload support in this profile.

## Retained qualification evidence

Results belong to the exact sources and artifacts named in their receipts. They do not automatically qualify later changes or every target platform.

| Area | Observed result and boundary |
|---|---|
| Local SDK and coordinators | Copied-consumer checks, generated configuration through real adapters with fake transports, 313 configuration comparisons, and 100 alternating-runtime repairs plus 100 reuses passed in the retained local campaign. These are deterministic and process checks. |
| Aggregate local checks | Clean source `f99e4b58c4ebaa1230a18a3e1cbb42999218e2dc` passed 37 recorded commands with unchanged source digest. Later focused changes require a new final aggregate disposition; this is not a current-head all-green claim. |
| Private installation | Copied-source and relocated compiled journeys passed on macOS arm64. Installed-copy acceptance removed its own incoming copy, used installed capability paths and rechecked inventories. This was a fresh directory on the existing machine, not a clean operating-system installation. |
| CLI bridge | Each final 61-case report records 44 full observable passes, 14 passes with explicit observation limitations and three unexecuted cases. Three repetitions per fixed-image CLI added 348 executed cases with no failures or retries, including 84 cases with explicit observation limits; 18 cases remained unexecuted. Windows was not run; two opaque-routing cases are covered by separate parser tests. Binding-open syscall ordering and some image/configuration isolation assertions are not independently proved. |
| Real coordinator delegation | Eight compiled CLI/coordinator combinations passed 80 commands covering repair, reuse, bounded serving, unknown effects, refusal to replay and both settlement outcomes while preserving attempts. No provider was called. |
| Generated bridge setup | Four CLI/coordinator pairings passed 28 printed-command journey commands. An independent fresh-user walkthrough succeeded; optional installed-helper discovery and safely quoted commands address its reported friction. |
| Fixed-image native CLI | Fresh Rust and Bun artifacts passed the bridge corpus and image admission checks. Actual adapter preflight used fake credentials and intercepted normal model execution. It does not establish authenticated provider access or live fulfillment. |
| Rust CLI regression profile | Test-only repair `1cab860` passed 52 ordinary CLI tests and both targeted sentinel placement tests. Earlier failing reports remain retained. Production behavior and the retained release-profile binary were unchanged by this repair. |

The bridge implementation is identified by `8ca9f2caa983083eb6f402fc64a4865e32fe57b8`; setup/receipt improvements are recorded at `f99e4b58c4ebaa1230a18a3e1cbb42999218e2dc` and `3ed57c9`. These are local source references, not published downloads. A fixed-image Bun receipt distinguishes its embedded ancestor identity from the later actual build checkout; file and artifact hashes are the evidence for that build.

Use the [aggregate qualifier](../experiments/weave-seed/integration/qualify.py), [independent bridge process harness](../cli/shared/tests/weave_host_process.py), [shared bridge corpus](../cli/shared/fixtures/weave-host-v1.json) and [bundle guide](../experiments/weave-seed/distribution/README.md) for repeatable checks. The private IMP-017 lab retains `docs/CLI-BRIDGE-READOUT.md` and `docs/LOCAL-IMP026-READOUT.md`, with linked reports and prior failures. Those lab records are not bundled public evidence; this document does not invent external download links for them.

## Semantic assessment remains unresolved

An earlier live campaign used the real kernel, Jev `jev-1.13.0` and an Agents SDK agent across finite Bun and Rust sequences. Correct artifacts did not imply complete compliance: the shell-capable actors made prohibited Git calls in two of six actions. Artifact-only assessment lacked those operation observations. Subsequent trace disclosure, question narrowing and parsed-trace diagnostics did not reliably detect the violations. These negative cases remain required evidence, not superseded successes.

A separate restricted file-tool profile passed all ten event checks across the two sequences. Removing shell/process tools restricted available effects; it did not demonstrate that the classifier can certify arbitrary procedures. The profile was a trusted laboratory harness, not an operating-system sandbox or the final portable packaged actor.

The current local product has not had a new live BYOK campaign against its final installed artifacts. Assessment research must distinguish insufficient inputs, evidence representation, question design, decision policy and model errors. Artifact correctness, required operations, reporting duties and permission compliance need separately observable evidence and independent labels. Decomposition or clearer questions are testable proposals, not established fixes. The retained failures do not establish that the classifier approach is impossible; they prevent claiming general reliability today.

## Remaining gates

| Gate | Required work or decision |
|---|---|
| Architecture guard | The scoped repair passes the repository guard and all 34 checker tests, including negative regressions. It admits exact reviewed files, imports and one pinned dependency; it corrects read-argument parsing without exempting the runner. This is a structural check, not a security audit. |
| Final integrated acceptance | Rerun qualification for the selected final source, copied bundle and installed artifacts; retain exact identities and all failed runs. Focused passes are not a substitute for the final aggregate. |
| Semantic and live BYOK acceptance | Run a separately authorized, finite campaign with complete agreement/evidence identities, independent operational review, uncertainty handling and the original negative cases. Preserve spending reservations and unknown effects. |
| Platform and provisioning | Qualify the advertised target matrix and native harness installation. Current compiled evidence is macOS arm64; process supervision is Unix-specific. Existing cached dependencies are not evidence of a fresh network installation. |
| Release review | Review API compatibility, dependency/security boundaries, support and feedback ownership, upgrade behavior and publication authorization. Private package versions and local builds are not a v1 release. |
| Account and hosted work | IMP-027/028/029 cover account flow, hosted transfer and publication. Reuse existing authentication/services where possible; any required backend addition needs a separately reviewed Raymond-approved change. No backend deployment occurred here. |

Local locks coordinate cooperating processes; they do not provide distributed ownership, exactly-once effects or an operating-system sandbox. Checkpoint durability has documented filesystem and power-loss limits. Bun's synchronous coordinator capabilities may wait for their bound during cancellation; Rust can interrupt an active child. Bridge supervision does not erase that host-level distinction or authorize clearing a conservative lock.

Continue through the current guides and [manual feedback template](../experiments/weave-seed/FEEDBACK.md). Evidence and secrets are not uploaded automatically. No publication, version promotion, release-branch merge or kernel migration is authorized by this readiness assessment.
