# Real-harness research lane

This directory contains an opt-in, cost-bounded way to observe real model
routes without turning a successful model response into a stronger claim than
it supports. The frozen matrix and policy were written before the observations
under `evidence/current/` were collected in the original repository. Those
records are historical and are not copied into this public client. Current
evidence must carry its measured capture and settlement fields; the old
in-place enrichment command is retired. Use the original immutable source to
interpret an earlier record rather than rewriting it to a newer schema.

The four evidence levels are intentionally separate:

1. **Exploratory route canary**: a direct harness call attempted one harmless
   exact-token task through the selected provider/model route. Its pass/fail is
   recorded separately as the observation classification.
2. **Base transport observation**: not tested here. Parseable direct harness
   JSON is not evidence about an exact CLI adapter transport boundary.
3. **Strict wrapper admission**: not tested here. The installed `prime-agent`
   is 0.7.0 while the stable adapter recipe pins 0.8.1.
4. **Semantic conformance / Prose Complete / release eligibility**: unknown or
   false. The canonical Skill Runtime Image and terminal schema do not yet
   exist, so this lane cannot award those claims.

Direct `prime-agent` observations are never described as wrapper admission.

## Safety and evidence properties

- Real calls require both explicit CLI acknowledgements.
- The whole frozen matrix has a $1.00 planned ceiling, one trial per route,
  zero retries, and a hard per-trial timeout.
- Every run uses a new disposable working directory and disables tools,
  built-in tools, extensions, skills, context files, prompt templates, themes,
  thinking, and session saving.
- Provider and model are explicit. Only credential names declared by that
  route are read from the env file; other credentials are not placed in the
  child environment. Prime-hosted routes use the existing stored connection
  and receive no env-file credentials.
- Each stdout and stderr reader enforces the frozen 8 KiB per-stream policy cap
  while the child is running. The first over-limit chunk terminates and settles
  the original process group and produces an explicit `output-limit`
  observation. Evidence records the cap, observed byte counts, whether live
  enforcement occurred, and hashes/sizes of only the bounded captured prefixes.
  Persisted diagnostics are scrubbed of the generated prompt, known credential
  values, common secret forms, the disposable path, and the local home path.
- Prompt bodies and credential values are never written to evidence. Failure
  workspaces are deleted; the sanitized failure observation is retained.
- Usage and cost are recorded only when the harness emits corresponding
  numeric JSON fields. Harness-reported USD amounts are not mislabeled as
  billing-authoritative spend. Missing cost remains unknown; planned ceilings
  are not mislabeled as actual spend.
- Terminal assistant attempts are counted. The first frozen matrix exposed
  that prime-agent 0.7.0 internally retried one OpenRouter provider failure
  even though this runner launched only one trial with zero runner retries.
  That observation is retained and marked as a retry-policy violation.
  Subsequent runs stream JSONL and terminate the whole harness process group
  immediately after the first terminal assistant failure, preventing the
  harness from issuing a second model attempt.
- On POSIX, timeout, output overflow, interrupted execution, and normal leader
  exit all settle the exact process group created for the harness and close both
  capture pipes. This is process-group cleanup, not strict containment: a
  malicious or accidental descendant that calls `setsid(2)` can escape the
  original group. Every evidence record therefore sets
  `detachedDescendantsContained` and `strictContainmentClaimed` to false. The
  retained 2026-08-27 observations predate live cap enforcement; their migrated
  evidence says `enforcedLive: false`, leaves the overflow result and original
  group emptiness unknown, and does not retroactively infer either fact.

## Provider-free test gate

```sh
python3 -m unittest -v cli/conformance/real-harness/test_run.py
python3 cli/conformance/real-harness/run.py validate
python3 -m json.tool cli/conformance/real-harness/policy.v1.json >/dev/null
python3 -m json.tool cli/conformance/real-harness/matrix.v1.json >/dev/null
```

The tests use `fake_prime_agent.py`; they make no provider or network calls.

Prior research logs are inspected only through `prior_metadata.py`. It reads a
bounded prefix from at most eight explicitly named files and emits only source
size/hash metadata and model IDs—never transcript lines or prompt content.

## Explicit real probe

```sh
python3 cli/conformance/real-harness/run.py run \
  --output-dir cli/conformance/real-harness/evidence/current \
  --executable /absolute/path/to/prime-agent \
  --env-file /absolute/path/to/credentials.env \
  --i-understand-this-spends-money \
  --accept-research-only-incompatible-version
```

Select a subset with repeated `--route ROUTE_ID`. A failed or unavailable route
is written to evidence and the remaining frozen routes continue without retry.
The command returns nonzero if any selected route does not pass its exact-output
canary.

The executable is always an explicit absolute path. `--env-file` is required
only if a selected route declares credential names; the runner never discovers
an ambient `.env`. Neither local input path is written to evidence. A
Prime-hosted-only selection therefore needs no credential file and continues to
use the separately configured Prime login described above.

Regenerate the derived reports deterministically:

```sh
python3 cli/conformance/real-harness/run.py report \
  --evidence-dir cli/conformance/real-harness/evidence/current \
  --output-json cli/conformance/real-harness/evidence/current/report.json \
  --output-markdown cli/conformance/real-harness/evidence/current/REPORT.md
```
