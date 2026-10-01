# Local file-polling coordinator

This Bun sidecar embeds the existing integration `runConfig` and trusted-local checkpoint host. It is not a public top-level Prose command, Markdown interpreter, HTTP server, distributed ownership service or hosted account. Local explicitly selected capabilities can use the caller's own provider keys; this coordinator neither selects a provider nor requires an OpenProse login.

From the repository root, using Bun 1.3.5:

```sh
bun --no-env-file experiments/weave-seed/local/run.mjs --help
bun --no-env-file experiments/weave-seed/local/run.mjs check /absolute/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs step /absolute/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs status /absolute/config.json
bun --no-env-file experiments/weave-seed/local/run.mjs serve /absolute/config.json --poll-ms 1000 --max-steps 60
bun --no-env-file experiments/weave-seed/local/coordinator.test.mjs
```

`stepConfig`, `statusConfig`, `settleConfig`, `acquireOwner` and asynchronous `serveConfig` are exported from `coordinator.mjs`. Serve requires explicit bounds: poll milliseconds 1–3600000 and steps 1–1000000. It accepts an AbortSignal and optional onStep callback. Callbacks are trusted embedding code. The coordinator returns a summary; it does not retain an unbounded history. CLI output is JSON per completed step and one stop record, diagnostics go to stderr, and thrown errors yield nonzero exit.

## Offline configuration check

`check CONFIG` is available here through Bun and through the separate [native Rust coordinator](../rust-local/README.md). It shares `prepareConfig` with actual execution, then observes the selected files, checks executable-file access without launching commands, reports selected and missing environment variable names without values, and reads checkpoint/lock diagnostics. It does not create files or locks, load dotenv files, invoke an executor or evaluator, or contact a provider. Invoke Bun with `--no-env-file` as shown above.

Its single JSON record uses schema `openprose.weave-check/1`, status `configured` or `blocked`, stable error codes and explicit `providerVerified: false` and `semanticAssessment: false`. Exit 0 means configured; exit 2 means blocked. `--help` exits 0 and prints usage. A missing checkpoint is normal; pending state, corrupt/unreadable state, existing locks, missing variables, source gaps and unavailable executables block the check. Source diagnostics use the same bounded observer as execution. Runtime name/version is descriptive, not an unsupported-platform qualification claim.

This check does not run executable `--version` or native/provider readiness, validate arbitrary child-script arguments or provider-specific configuration, verify credentials, establish billing/spending bounds, or prove contract compliance. `configured` is a read-only local observation, not permission to act or a reservation of state. Files and environment can change before execution. Provider adapters still perform their own byte-binding/configuration checks when invoked. Status remains a narrower checkpoint diagnostic and can succeed for a configuration that would not execute.

## Configuration and a fully local example

The configuration is exactly the existing integration schema; see `../integration/README.md`. Source paths resolve from `root`, while root and checkpointDirectory resolve from the configuration's directory. Executable names must be absolute; all other argv are literal. `environmentKeys` explicitly selects already existing parent variables; literal environment values are rejected. Configuration is trusted executable policy, not task data. Never put credentials into selected source/evidence files or configuration.

For a provider-free example, create a fresh directory containing kernel.md (`Synthetic kernel`), program.md (`Synthetic source/report equality`), source.txt (`one`), report.txt (empty), and config.json below. Replace both executable placeholders with the absolute Bun executable and both fixture placeholders with this repository's absolute `experiments/weave-seed/local/fixture.mjs` path. This fixture is deliberately synthetic; it is not OpenProse semantic assessment.

```json
{
  "schema": 1,
  "root": ".",
  "kernel": "kernel.md",
  "contracts": ["program.md"],
  "evidence": ["source.txt", "report.txt"],
  "capabilityVersion": "synthetic-v1",
  "assessor": ["/absolute/bun", "/absolute/repository/experiments/weave-seed/local/fixture.mjs", "assess"],
  "actor": ["/absolute/bun", "/absolute/repository/experiments/weave-seed/local/fixture.mjs", "act"],
  "environmentKeys": [],
  "checkpointDirectory": "host",
  "maxAttempts": 3,
  "ttlMs": 60000,
  "timeoutMs": 2000
}
```

The fixture repairs report.txt to source.txt and records assess/act calls in calls.log. First step reaches satisfied; another unchanged fresh step returns reused without another capability call. Change source.txt during serving to cause reassessment/repair; delete it for evidence-gap without capability invocation. Each successful action consumes the same persisted cumulative budget. A report obligation must itself be included in selected evidence; this example does not supply such semantics automatically.

Evaluator configuration/question file contents and other semantic policy inputs **must be explicitly included in contracts/evidence** if argv references them. Existing runConfig hashes argv/environment/capabilityVersion, not arbitrary files named by argv. Changing capability executables or their transitive dependencies requires a new capabilityVersion or another explicit identity binding. No general dependency discovery or immutability sandbox is provided. The coordinator freezes configuration bytes during a service session and fails before the next step on any change; restart after reviewing changes. Filesystem mutation between the check and read is outside the trusted-local profile.

## Ownership, persistence and recovery

`service.lock` under checkpointDirectory serializes coordinator step/serve entry points and is held for the whole service, including idle polls. The existing `lock` separately protects each underlying host step. Status is a non-mutating diagnostic and creates no directories; its ownership/checkpoint values are not an atomic snapshot or authorization to act. A missing checkpoint is reported as null; corrupt checkpoints throw. Existing locks are never deleted automatically, even when a PID appears dead. Owner release checks directory device/inode identity and refuses to remove a replacement lock.

The service lock is advisory between these coordinator entry points. Direct calls to the older integration runConfig/FileHost bypass it, so all cooperating local controllers must use the coordinator for service-level exclusion. There is no distributed lease, stale-owner fencing or hostile-filesystem protection.

Every poll performs the existing bounded file observation. Unchanged fresh satisfaction avoids model/process calls; expiry triggers reassessment. Gaps and unknown never authorize action. An action error preserves pending; serve stops with pending instead of retrying. Restart exposes recovery-needed without replay. Evaluator/save/configuration errors propagate; the service lock is released on orderly exit, while any underlying uncertain-durability host lock remains intact. No budget reset, settlement shortcut or automatic stale-lock recovery is added. Recovery requires trusted explicit investigation and the existing host settlement contract; follow [the recovery guide](RECOVERY.md).

SIGINT/SIGTERM request cancellation and interrupt idle waits. Current child capabilities use synchronous spawnSync, so signals cannot reliably preempt a running step in JavaScript; the configured child timeout remains the available bound. It kills the direct child, not every descendant, and external effects may already have happened. Hard process termination may leave service/host locks for trusted reconciliation. No promise of instantaneous cancellation, whole-process-tree containment, atomic multi-file snapshots, power-loss recovery or provider spend enforcement is made.

## Validation

The tests use real temporary files and subprocesses, an empty child environment and no network/provider credentials. Checks cover readonly status/corruption, repair and fresh reuse without calls, cumulative restart budgets, separate-process duplicate ownership, service ownership across idle polls, actual CLI SIGTERM during idle, expiry reassessment, source mutation, missing evidence, effect-then-failure pending/restart, abort cleanup, retained existing locks, configuration mutation, propagated evaluator errors, invalid bounds and forbidden literal environment. These are coordinator mechanics tests, not model-backed semantic or installed-product qualification.

## Explicit settlement after investigation

```sh
bun --no-env-file experiments/weave-seed/local/run.mjs settle /absolute/config.json \
  --binding EXACT_STORED_BINDING --attempt EXACT_PENDING_ATTEMPT \
  --outcome completed --receipt REVIEWED_RECEIPT_REFERENCE
```

The fixed-order outcome is `completed` or `not-applied`, chosen by the trusted operator after investigating the actual effect. Neither outcome is inferred from a timeout or executor exit. `settleConfig(path, binding, attempt, outcome, receipt)` returns the updated checkpoint; CLI output is `{ "status": "settled", "attempts": N, "pending": null }`. The function acquires the service-owner lock, then the existing checkpoint host lock, and delegates exact binding/attempt/outcome/nonblank-receipt validation to FileHost. It clears pending and cached satisfaction, retains attempts, and records the settlement. A subsequent execution must observe and assess again.

Recovery reads only configuration schema/checkpointDirectory and the stored checkpoint; it requires no source availability, provider keys or valid executor/evaluator executable. It invokes no capability or provider. Mismatched identities, repeated settlement, blank receipt and any existing owner/host lock fail without resetting state. This is not an unlock command: a stale or uncertain-durability lock still requires separate trusted reconciliation under [RECOVERY.md](RECOVERY.md). Settlement is a trusted assertion with a receipt reference, not automatic receipt authentication or proof of fulfillment.

Run the five additional provider-free settlement checks with `bun --no-env-file experiments/weave-seed/local/settlement.test.mjs`. They verify both outcomes, exact-byte preservation on rejection, unchanged budgets, missing environment/source independence, service/host lock refusal, and actual CLI fixed-order parsing with an empty environment.
