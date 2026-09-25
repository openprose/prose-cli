# Experimental weave core (IMP-017)

This headless Python reference explores assess/act over caller-bound evidence. It is not a shipped CLI command, Markdown interpreter, or published SDK. The original planning proposal used IMP-016; workspace IMP-017 resolves that ID collision.

`reconcile(binding, checkpoint, observe, assess, act, save, clock, new_id)` accepts ordinary capabilities. Observation reads a declared file or SQLite result; assessment and action are the two intelligent roles. Contract resolution remains external. The binding string must identify the contract, evidence selector, assessment policy and permissions; changing any invalidates reuse.

One call performs at most one action. It records a pending attempt before action, then rereads and reassesses; a normal actor return is not fulfillment. Pending attempts require explicit recovery instead of replay. The host owns scheduling, durable checkpoint storage, serialization and action budgets. `max_attempts` is cumulative for the supplied checkpoint, not automatically replenished after a failure or new event.

SQLite results use multiset semantics: row order is ignored, duplicate rows retained, column names and query/parameters bound into identity. Ordered output needs a different selector. A query result is a projection; an omitted column cannot influence a judgment. File reads are UTF-8 and bounded. Expiry is separate from content identity; an unchanged digest does not establish external-source freshness.

Run `python3 -m unittest discover -s experiments/weave -p 'test_*.py' -v` from the repository root. Cases are specified in CASES.md. Tests use fresh local state and fake assessment/action; they establish no model reliability. The final reread detects tested races, but cannot make a remote world atomic or eliminate a change after return. No exactly-once guarantee is claimed.

Research evidence is kept outside this repository; the user-authorized campaign ends September 18 at 08:00 Eastern. No release or foundation promotion follows automatically.

Interrupted actions can be settled through `FileHost.settle(binding, attempt, outcome, receipt)`. The trusted host must establish `completed` or `not-applied` independently; unresolved effects remain pending. The method matches the recorded binding and attempt, atomically stores the last settlement reference, clears pending, and invalidates cached satisfaction. It neither invokes work nor replenishes attempts. The next `step` observes and assesses normally. A classifier score alone is not a settlement receipt. Receipt authenticity and an append-only audit history remain host responsibilities; the checkpoint retains only the latest settlement. This interface is an experimental local primitive, not distributed recovery or exactly-once execution.

`query_evidence(..., named_rows=True)` emits one object per row with column-name keys, preserving nulls and duplicate rows. It uses the distinct `sqlite-unordered-named-rows-v1` selector identity; the default positional format is unchanged. Duplicate column names produce an evidence gap, requiring explicit unique SQL aliases. Byte bounds apply after conversion. Neither representation establishes completeness or source freshness.

## Try the headless loop

```sh
python3 experiments/weave/demo.py
```

This temporary SQLite demonstration needs no credentials or network. It prints five events: initial satisfaction, reuse after an irrelevant note edit, repair after a date change, reuse on a duplicate, and abstention after approval is revoked. Each event reconstructs the local host from its saved checkpoint. Assessment and action are deliberately deterministic here; live model evidence is in the lab, not hidden in this demo. Its temporary files are removed on exit.

The proposed reusable boundary is `host.step(binding, observe, assess, act, clock, new_id, max_attempts)`. A caller supplies the three capabilities; the loop controls when they run. Observe is ordinary input preparation, while assess and act can use different providers. Contract/package resolution, event subscriptions, authorization, credentials and cost accounting remain outside this reference core. No new Markdown grammar or shipped CLI command is introduced.

The file observer accepts regular UTF-8 files, including symlinks resolved to regular files. It rejects special files before reading and checks the opened descriptor again; nonblocking open avoids waiting on a FIFO swapped in after the initial check. This is a POSIX local reference, not a general filesystem sandbox or network-filesystem deadline guarantee.

Checkpoint filenames ending in `.lock` or `.tmp` are rejected because those suffixes are reserved for store sidecars. Checkpoints sharing a stem with different other suffixes share the local lock and serialize conservatively. Configure a dedicated trusted checkpoint directory; this store does not defend against hostile filesystem writers or symlink manipulation.
