# Local evidence walkthrough

The [Bun example](local.mjs) and [Rust example](local.rs) implement the same tiny synthetic date rule. Follow the [seed quick start](../README.md#first-local-run) to execute them. Both save a pending checkpoint before changing in-memory state, reassess the repaired state, and reuse satisfaction on a second call. Their assertions check these outcomes. They contain no kernel loader or automatic prose binding; the separate program below illustrates the actual language entry route.

Run the existing Python example from the repository root:

```sh
python3 experiments/weave/demo.py
```

This uses the Python reference host, not an installed Rust/Bun package. Read [demo.py](../../weave/demo.py) for the complete executable example. It creates one SQLite row with a desired date, an actual date, approval, and an unrelated note. The observer selects `desired`, `actual`, and `approved`; the note is deliberately outside the selector. A deterministic evaluator checks approval and compares the two date values. The executor copies the desired value to the actual value in the temporary database.

| Event | Expected status | Cumulative assessments | Cumulative actions | Meaning |
|---|---|---:|---:|---|
| Initial state | `satisfied` | 1 | 0 | The selected row meets this fixture's rule. |
| Irrelevant note edit | `reused` | 1 | 0 | The selected content and binding are unchanged. |
| Desired date changes | `satisfied` | 3 | 1 | Assessment requests work, then fresh evidence is assessed after action. |
| Duplicate event | `reused` | 3 | 1 | A new event alone does not invalidate the selected evidence. |
| Approval is revoked | `unknown` | 4 | 1 | The evaluator cannot authorize work under this fixture's rule. |

The host is reconstructed from its saved checkpoint for each event. Temporary files are deleted when the example ends. It makes no model or network calls and does not collect or upload feedback.

The example demonstrates only this narrow obligation. It does not prove that arbitrary prose contracts can be assessed reliably, that approval data is authentic, or that a real service was repaired. An unchanged query result can also omit a relevant field. If the real agreement requires a per-event report, audit record, or prescribed operation, the host must include that obligation in the assessment scope or execute it separately. The fixture must not be reused as an implicit definition of contract fulfillment.

For an integration, first resolve the selected kernel and agreement, then declare the complete evidence selector and permissions. Supply observation, assessment, action, persistence, clock, and attempt-identity capabilities explicitly. Keep evidence gaps distinct from a judgment that work is needed. For an uncertain action, inspect actual effects and follow the host's recovery protocol before another attempt.

## A kernel-backed prose example

[brief.prose.md](brief.prose.md) is a separate OpenProse program with synthetic [notes](notes.md). It explicitly adopts the canonical result and source-grounding conventions at source revision `7dc90670b4ccd862b7d0939a75d8b819b03a2b1b`. It owes a brief, factual support, a claim check, and an honest fulfillment report. It does not adopt the Python fixture's assessment rule.

For a direct agent session, supply that program, its notes, the selected kernel, and the exact adopted definitions. The inspected kernel source for this example is [the same pinned core revision](https://github.com/openprose/openprose-language/blob/7dc90670b4ccd862b7d0939a75d8b819b03a2b1b/README.md). The agent reads and enacts those Markdown agreements using its ordinary tools. Merely reading this walkthrough does not execute the program.

The existing CLI offers a different entry route: an ordinary published-kernel build retrieves and verifies the published kernel, appends it to native harness instructions, and sends the task separately. Read [kernel startup](../../../docs/kernel-startup.md) and the selected build's documentation first. A launch pattern, requiring an installed admitted harness, a model available to your account, and explicit authorization for provider use, is:

```sh
/path/to/prose --harness codex --model MODEL --permission-mode workspace-write \
  --cwd /absolute/path/to/disposable-example-copy --output-contract native \
  run brief.prose.md
```

This pattern was not executed in the seed's provider-free validation. Copy the example files into a disposable working directory before any authorized live run. Ordinary published-kernel startup selects the verified published kernel for that invocation; it does not automatically pin the kernel to this example's inspected source revision. Exact kernel selection requires the existing verified-image build route described in the startup guide. Do not claim that a project lock selects the runtime kernel when that behavior is absent in this source revision.

Neither entry route automatically binds the Rust/Bun seed to the program. A future integration must explicitly resolve the effective agreement and bind evidence for the brief, source support, required verification, and reporting. The seed's internal `unknown` disposition is not a third public outcome under the adopted result convention. A successful native process exit also does not independently establish fulfillment.
