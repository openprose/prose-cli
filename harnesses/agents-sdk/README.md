# Generic OpenAI Agents SDK local harness

A normal `Agent` + `Runner` loop with one general shell tool. The model chooses every tool action. This harness treats instruction-file contents as opaque text and has no knowledge of Contracts, references, programs, acceptance, state, or libraries. It is an optional bring-your-own-harness test implementation, not an OpenProse dependency.

## Install and run

Use Python 3.10 or later:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py --model MODEL --cwd /path/to/workspace --prompt 'Read README.md and perform the requested task.'
```

Supply `OPENAI_API_KEY` in the process environment or pass `--env-file /private/path/.env`. Only OPENAI_API_KEY is loaded from that file. An optional `--instructions FILE` appends opaque instructions to a generic coding-agent instruction. The working directory is identified in instructions. No file is automatically discovered or interpreted.

JSON lines on stdout record starts, actual tool commands and results, final text and token usage, or error type. Exit 0 means Runner returned a final response, **not that the requested task passed acceptance**. Observers must inspect results independently. Tracing export is disabled. Exceptions expose types rather than potentially sensitive request bodies.

Defaults: 180 second overall timeout, 30 seconds per shell action, 20 Runner turns, 12,000 output tokens per model request. Shell timeouts and cancellation terminate the shell process group. Tool stdout/stderr are each truncated to 30,000 characters. No hidden retries, output repair, adjudication, or program-specific policy is supplied by this wrapper; SDK/provider transport behavior remains upstream behavior.

## Environment boundary

The shell is ordinary local bash with a supplied working directory. **This is not an OS sandbox:** use a separately isolated machine/container for untrusted programs. Keys/tokens/secrets/password variables are excluded from the shell environment, but the shell can still read host files within OS permissions. This wrapper adds no filesystem confinement. Tests use synthetic data in separate copied workspaces.

No web, subagent, or notification tool is provided in this initial profile. Unsupported program requirements must be handled by the model rather than silently provided by an evaluator.

## Validation

```sh
.venv/bin/python test_run.py
```

Local tests exercise actual read/write/exit behavior, process-group timeout cleanup, budget forwarding, safe failures, and provider-free usage accounting. A mocked provider request comparison verifies that raw-usage preservation changes neither request fields nor the tool schema. Initial real-model observations are in `EVIDENCE.md`. No language acceptance tests are embedded in the harness.

Implementation followed the official [Agents SDK migration example](https://developers.openai.com/cookbook/examples/agents_sdk/migrate-from-claude-agent-sdk/readme) and inspected installed SDK signatures. Pinned direct dependencies: OpenAI Agents SDK 0.22.2, OpenAI Python 3.13.0, python-dotenv 1.2.3.

The outer runners expose explicit `--native-max-turns` and `--native-timeout` selections; see [SDK execution budgets](../../docs/sdk-budgets.md). Native start/error records report configured limits. Safe native error types distinguish turn exhaustion and timeout; all other exception classes become `ExecutionError`, without exception bodies.

Final and error records also carry an additive `usageObservation`, collected through public SDK `RunHooks` with `ModelSettings(preserve_raw_usage=True)`. Existing successful `final.usage` remains the SDK's normalized aggregate. The new observation sums only allowlisted nonnegative integer counters actually present in raw usage on unique completed model responses: input/output/total tokens, cached/cache-write input tokens, and reasoning output tokens. Missing or invalid counters are absent, never inferred as zero. `fieldResponseCounts` reports how many completed responses supplied each counter; observed totals can therefore cover only part of a run. These totals overlap legacy `final.usage`: **do not add them together**. `aggregationScope` identifies the per-run completed-response aggregate, excluding cumulative context snapshots. Provider response/request IDs are used only for internal deduplication and are not emitted; identical callbacks without IDs are deduplicated by object identity.

`startedCallCount`, `completedResponseCount`, and `outstandingCallCount` refer to logical SDK hook calls, not provider transport requests. `duplicateResponseCallbackCount` records ignored duplicate completion callbacks. Transport retries, responses without preserved raw usage, cancelled requests, and any provider work invoked separately from shell are outside this observation. Accordingly `outstandingProviderRequestCount` is null and `totalRunUsageKnown` is always false, including successful runs; these observations cannot establish total billing or justify releasing a failed run's spending reservation. An error before any observed response yields empty token totals, not measured zero usage. Ordinary SDK failure, turn exhaustion, or overall timeout preserves completed-response observations in the error record. External process termination before final/error emits no such summary. No new JSON event type, tracing export, model instruction, tool, budget, or protocol version is introduced.
