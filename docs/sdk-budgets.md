# Agents SDK execution budgets

Both runners accept these optional configuration controls for `--harness agents-sdk`:

- `--native-max-turns N`: positive safe integer. Default 20. The allowance is aggregate across parent and fresh child contexts. Function-tool calls have a separate limit.
- `--native-timeout TIME`: positive duration using `ms`, `s`, `m`, or `h`. Default 180s. This controls the SDK harness's inner whole-run deadline and model request timeout.

- `--native-tool-timeout TIME`: positive duration with the same units. Default 30s. This controls each SDK shell or public-retrieval action; an explicit `180s` forwards the existing helper option `--tool-timeout 180`.

`--timeout` remains the independent outer runner deadline. The first applicable limit wins. Setting an outer deadline longer than 180s does not silently extend the SDK deadline. For example, `--timeout 10m --native-timeout 8m --native-max-turns 60` grants up to 60 SDK turns and 480 seconds inside the 600-second outer limit. These flags do not guarantee completion, add retries, or change the task. Other harnesses reject explicit native budget options instead of silently ignoring them.

Quoted TOML keys are `native_max_turns="60"` and `native_timeout="8m"`, and `native_tool_timeout="180s"`; environment variables are `PROSE_NATIVE_MAX_TURNS` `PROSE_NATIVE_TIMEOUT`, and `PROSE_NATIVE_TOOL_TIMEOUT`. Normal flag > environment > project > user precedence applies. Zero, negative, malformed, infinite or overflowing values are rejected. Omitted values preserve the existing native launch defaults.

SDK invocation, dry-run, explanation and result records expose eleven `nativeLimits` fields: `maxTurns`, `timeoutSeconds`, `toolTimeoutSeconds` (default 30), `maxOutputTokens` (12000 per model request), `maxAggregateRequests`, `maxAggregateHostedWebCalls`, `maxAggregateFunctionTools` (80), `maxObservedTotalTokens` (500000), `maxRequestInputBytes` (256000), `maxChildren` (8) and `maxChildDepth` (1). Aggregate requests and hosted calls inherit `maxTurns`; hosted tools are capped at one per model request. Historical four-field diagnostics remain readable. The generic SDK harness also reports its configured limits in start and error records. These are configured allowances, not measured consumption. The harness's direct `--max-turns`, `--timeout`, and `--tool-timeout` arguments remain available without either outer runner.

Normalized native failures carry a closed `nativeFailure.kind`: `max-turns`, `timeout`, or `execution`, with validated numeric limits and elapsed seconds when supplied. Arbitrary exception names and bodies are not passed through. A shell-action timeout is a tool result the agent may handle; an SDK run timeout terminates that run; outer cancellation has its existing separate error code. Cleanup signals do not replace the recorded native cause. A file written before a turn limit is reached remains an artifact, not proof of a completed invocation. Success still requires a native final record and successful process settlement.

Increasing the tool allowance does not increase the SDK parent or outer deadline. Omission forwards no extra tool-timeout argument; explicit `30s` reports its selected configuration source while preserving the same effective allowance. A tool timeout can return a tool error for the agent to handle, not guaranteed cancellation of every external effect. `delegate(task)` starts an independent SDK Agent/Runner conversation with only the explicit task, the same opaque instructions and shared working directory. It inherits no parent conversation or tool history. Children execute serially and cannot delegate; their calls, tools and observed usage consume the shared invocation allowances. Files remain shared. Whole-run timeout and cancellation cover active parent/child contexts and ordinary tool process groups; detached descendants escaping a group are outside that guarantee. No OpenProse-specific interpretation is introduced.

The request input-byte guard measures actual serialized instructions, history,
tools and output schema before transmission. The observed total-token ceiling
stops the next request; it cannot interrupt or price an in-flight request. Safe
`usageObservation` and `modelIdentity.serviceTier` diagnostics preserve completed
observations across success and failure, while normalized `usage` remains
unavailable. Missing counters remain absent, provider outstanding requests remain
unknown and `totalRunUsageKnown` remains false. These are execution allowances and
observations, never an authoritative currency/billing ceiling. Explicit API-client
and SDK retry settings disable transport and conversation-lock retries.
