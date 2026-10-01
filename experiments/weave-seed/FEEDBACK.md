# Report a reproducible Weave problem

First reproduce the problem with synthetic local evidence and no provider calls if possible. Copy the template below into the repository's existing issue or review channel after reviewing it yourself. This file neither creates an issue nor uploads logs. Ask a maintainer for the appropriate private channel when a report contains a security issue or cannot be reproduced without sensitive data.

Keep only the minimum evidence needed. Remove credentials, authorization headers, account identifiers, private paths, customer records, and unrelated transcripts. Replace sensitive content with stable synthetic values while preserving the failure. Do not paste environment dumps, complete checkpoint directories, or full native logs. A content hash can identify a retained artifact but is not a substitute for permission to disclose it.

```text
Title: [observed failure in one sentence]

Surface: Python reference / Rust seed / JavaScript seed / documentation
Source revision: [exact commit; describe relevant uncommitted changes]
Runtime and version: [Python, rustc, Bun, or Node as actually used]
OS and architecture: [no hostname or account identity]

Expected behavior:
[Requirement and its source. Identify the selected contract/kernel revision
if relevant; do not attach private contract text without permission.]

Observed behavior:
[Status, exit code, number of assessments/actions, and visible effects.
Separate actual observations from inferred causes.]

Minimal synthetic input:
[Small fixture, evidence selector, binding changes, timestamps, attempt limit,
and initial checkpoint needed to reproduce. Never use live credentials.]

Exact reproduction commands:
[Run from the repository root; use portable paths. State dependencies.]

Actual output:
[Only the short, manually reviewed excerpt needed to show the failure.]

Repeatability:
[Attempts, failures, successful controls, and any race or timing dependency.]

Recovery and effects:
[Whether an action started, pending state remains, or an external effect is
uncertain. Do not retry an uncertain effect merely to obtain cleaner logs.]

Evidence limits:
[Unavailable logs, redactions, untested platforms, provider usage, and facts
that remain unknown. State whether the original failure is retained locally.]

Proposed scope:
[Loop, host/observer, assessment policy, kernel contract, adapter, or docs.
A suggestion is not an accepted semantic or release decision.]
```

Maintainers should acknowledge the report, reproduce or describe the missing information, assign it to the owning layer, and add a regression case before changing shared behavior. Close the loop by recording the tested revision and remaining limitations. Do not request secrets or treat a classifier's answer as independent proof of an external effect.

For the local sidecars, start with `check CONFIG` and `status CONFIG`. Check is offline and reports missing environment **names**, selected-source availability, executables and recovery state; it does not test provider authentication or model quality. Review the JSON before sharing it. A nonzero configuration-check exit can be useful evidence without exposing a provider key. Optional executor/Jev receipts stay local until you explicitly review and share a minimal excerpt.

If a defect can be reproduced without a provider, add the smallest failing case to the relevant coordinator, adapter or shared parity suite. Keep the original failure and identify the correction separately. A useful feedback cycle is: reproduce, assign the owning layer, add a regression, verify both runtimes where the behavior is shared, then record the accepted revision. No automatic upload is performed by the setup, check or qualification commands.
