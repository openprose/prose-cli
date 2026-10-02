# Explicit evaluator providers

An evaluator assesses whether the contract's requirements are satisfied using a result and its included or referenced evidence. This experimental process supports TypeSafe System One, OpenRouter Decisions, OpenAI Responses and Anthropic Messages through native adapters. It supplies the existing local runtime's assessment capability; it is not a new top-level `prose` command or a language interpreter.

The compatibility layer handles request serialization, versioned prompting where needed, constrained output and strict parsing. Evaluation policy is separate. Native probability distributions are retained for Jev; generative choice outputs have no invented confidence. Model explanations and automatic repair requests are not required.

## Configure explicitly

Author a configuration file using [config.example.json](config.example.json) and an evaluation plan using [plan.example.json](plan.example.json). Replace the illustrative model identifiers, credential variable and absolute contract paths with reviewed selections. `acceptedModels` is an exact allowlist of identifiers reported by that route, not a prefix match. Accepting a moving alias does not establish a pinned snapshot. Record alias use and choose an appropriate cache policy; the adapter cannot detect a silent model change behind an unchanged alias.

Inspect available adapters and validate configuration without calling a provider:

```sh
/absolute/bun --no-env-file /absolute/seed/providers/evaluation/run.mjs --capabilities
/absolute/bun --no-env-file /absolute/seed/providers/evaluation/run.mjs --check /absolute/source/evaluator.json
```

The check validates configuration and plan shape, limits and capability compatibility. It does not verify credentials, source bindings, provider access, model accuracy or independence. `--capabilities` also returns a digest-based `capabilityIdentity`. Include that identity with the executor and observer identities in the local host's explicit `capabilityVersion`; changing evaluator code must invalidate cached assessments.

In the existing local host configuration, select this process using its exact legacy field name:

```json
{
  "assessor": [
    "/absolute/bun", "--no-env-file",
    "/absolute/seed/providers/evaluation/run.mjs",
    "--config", "/absolute/source/evaluator.json"
  ]
}
```

This is a fragment of the existing host configuration, not a complete configuration. Include **both evaluator configuration and plan files in selected evidence**, inside the source root. Their raw bytes and canonical paths must match the observation. Pass only the required credential variables through the host's explicit environment allowlist; this process does not load `.env` files. Keep the executor selection and its required configuration separate. The earlier Jev-specific setup helper remains compatible with its original interface and does not generate this new configuration automatically.

The parent process timeout must cover the evaluator's total deadline plus receipt writing. The plan's evaluation count must fit `maxCalls`; calls are serial, with no fallback or retries. Byte and deadline bounds apply before/during transport. Input-token limits validate returned usage after a call; they cannot prevent a provider charge. Generative output limits are also sent to the provider. The caller owns campaign admission, reservations and cost reconciliation.

## Questions, contract instances and policy

Each evaluation identifies its subject contract instance, selected source paths and opaque bindings, a provider profile, named choice questions and a policy. All selected contract sources must be covered by at least one declared evaluation. This verifies declared coverage, not that the questions adequately express the requirements. The entire bound governing snapshot remains context; the subject identifies the scope of this evaluation.

Each choice maps explicitly to `satisfied`, `violated` or `unknown`. A label policy uses that mapping. A native-threshold policy additionally requires a unique highest probability meeting configured probability, confidence and margin thresholds; otherwise that question remains unknown. Profiles without native probabilities reject threshold policies before a request. No supplied threshold is certified as calibrated.

Several named questions can belong to one evaluation. Separately, multiple evaluations can assess different composed contracts or different instances of a reusable contract. This version supports only an explicit `all-required` plan. It does not infer conditional applicability or resolve conflicting requirements. Preserve required independence through the surrounding evaluation arrangements; separate calls or different providers alone do not prove independent review, and this adapter provides no independence attestation.

When all required questions are satisfied, the process returns `{"judgment":"satisfied"}`. If any question remains unresolved, it returns `{"judgment":"unknown"}`, even if another requirement is known unmet. The receipt retains that known violation as an assessment of nonfulfillment. When all assessments are resolved and at least one requirement is unmet, the legacy process result is `{"judgment":"work-needed"}`. The surrounding runtime and contract still govern whether subsequent execution is permitted. An assessment does not grant authority.

An evaluator can complete normally with unmet or unresolved subject requirements. Invalid input, refusal, incomplete provider output, invalid model/choice/usage, timeout or transport failure instead produces empty stdout, fixed `EVALUATION_FAILED` stderr and exit status 1. These retain the existing process protocol; language out/error are not renamed OS streams.

## Receipts and privacy

An explicitly supplied `receiptDirectory` must already exist as a canonical private directory. Each evaluation process writes a fresh mode-0600 receipt with configuration/plan/source and request/response digests, capability identity, per-evaluation subject identity, model identifiers, choices, optional native distributions, usage, policy and status. Completed assessments remain present if a later call fails. Raw prompts, source contents, response error bodies and credentials are excluded. Hashes are not anonymization. Required receipt failure is an evaluation error even if a provider has already charged.

Receipts do not replace a campaign ledger or provide a complete replay bundle. Retain authorized source snapshots and exact code separately when needed. The existing direct Jev adapter and its configuration remain supported; no legacy protocol names or saved records are migrated.

## Verification

```sh
bun test --no-env-file experiments/weave-seed/providers/evaluation/provider.test.mjs
bun test --no-env-file experiments/weave-seed/providers/jev.test.mjs
bun --no-env-file experiments/weave-seed/integration/evaluation-parity.test.mjs /absolute/weave-rust-local
```

The shared [specification](SPEC.md) and [fixture corpus](../../fixtures/evaluation-providers.json) define the observable controls. The aggregate local qualifier includes these tests. Ordinary tests use fake HTTP and synthetic local contracts. They do not establish live model accuracy or release qualification.
