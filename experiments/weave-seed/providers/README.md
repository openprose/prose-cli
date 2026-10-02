# Explicit Jev process evaluator

For interchangeable provider profiles, named questions and explicit composition of contract assessments, see the [evaluator provider guide](evaluation/README.md). This page documents the original direct Jev adapter, whose configuration remains supported.

This experimental adapter makes at most one TypeSafe Jev request for the existing synchronous [process capability](../integration/process.mjs). The child uses async HTTP; the parent still waits for one bounded process result. It has no account service, credential discovery, retry loop, executor, or automatic publication.

The process reads `openprose.weave-input/1` from stdin. Successful stdout is exactly `{"judgment":"satisfied"}`, `{"judgment":"work-needed"}`, or `{"judgment":"unknown"}` plus a newline. Invalid configuration, source binding, response, usage or transport causes exit 1, empty stdout and the fixed stderr message `JEV_ASSESSMENT_FAILED`. The parent must preserve that failure and must not reinterpret it as permission to act.

## Configuration and binding

Copy and review [jev.config.example.json](jev.config.example.json) and [jev.question.example.json](jev.question.example.json) into the selected source root. These examples are experimental policy inputs, not a recommended or calibrated assessment policy. In particular, a broad agreement question has failed retained lab negatives even when operation traces were supplied. A high reported probability or confidence does not establish correctness.

Configure the evaluator argument array as:

```json
["/absolute/path/to/bun", "/absolute/path/to/providers/jev.mjs", "--config", "/absolute/source/root/jev.config.json"]
```

Use an absolute config path. Filesystem aliases are resolved to canonical paths; configuration and question reads use a bounded file descriptor. `questionFile` resolves relative to that config file. Include **both config and question files in the selected evidence files**, within the observer's source root. They are data, not adopted contracts. Their exact raw bytes and paths must match the source snapshot, and the adapter checks the files again before HTTP. Arguments containing file paths alone do not bind file contents. Changing the question, policy, model, bounds or receipt configuration changes the evidence identity and must invalidate any cached judgment. Never put credentials in either file: these configuration files are sent to Jev as evidence along with the other selected evidence.

The source snapshot must include exactly one kernel source, at least one contract, and evidence sources. Every supplied source digest and the aggregate evidence digest must match. Source role labels are caller assertions; these checks establish byte identity, not source authority, adoption completeness or truth. The adapter does not discover adopted definitions or interpret Markdown itself. Supply all governing definitions explicitly through the existing binding interface.

Configuration fields are required:

| Field | Meaning |
|---|---|
| `endpoint` | Explicit HTTPS endpoint, without credentials, query or fragment. Example uses the observed TypeSafe `/v1/systemone` API. Redirects are rejected. |
| `model` | Exact `jev-X.Y.Z` version. Aliases are rejected; response must report the same model. |
| `apiKeyEnv` | Name of the one API-key environment variable, such as `TYPESAFE_API_KEY`. Only its name belongs in config. |
| `questionFile` | Reviewed JSON question with `scope`, choice `type`, `instructions`, and exact `satisfied`, `violated`, `unknown` criteria. Scope is descriptive metadata, not a provider instruction. |
| `decisionPolicy` | Explicit identity, minimum selected probability, minimum confidence and minimum margin over the next choice. All thresholds are in [0,1]. |
| `limits` | Positive timeout milliseconds, stdin/request/response byte bounds and returned input/output token bounds. Timeout may not exceed five minutes; each byte bound may not exceed 8 MiB. |
| `receiptDirectory` | `null`, or an explicit existing absolute canonical private directory with no group/other access. The adapter never creates a directory or chooses a default location. |

Supply the named key through the parent capability's explicit environment allowlist. No `.env`, shell profile, native login store or credential file is read. The adapter never inherits credentials into a model prompt intentionally and rejects a request containing the configured API-key value. Parent timeout should exceed the adapter timeout enough to permit response validation and receipt writing; the parent may still kill the process, so a missing receipt never proves no provider call occurred.

The endpoint is a trusted user configuration choice: selecting it authorizes transmitting the selected agreement and evidence to that endpoint. This implementation does not authorize calls by itself, enforce a campaign dollar budget, or establish provider retention guarantees. The caller must admit each call, reserve budget, and choose permitted data before starting the capability. Token limits validate returned usage **after** a request; they do not prevent a provider charging for a rejected result. There is no hardcoded pricing or automatic retry.

## Decision and evidence policy

A declared gap, a future observation or expired evidence yields unknown without contacting the provider. A response arriving after evidence expiry also yields unknown. Malformed evidence fails closed instead of becoming a fabricated assessment.

The response must contain the expected model, one choice answer, all three finite probabilities approximately summing to one, finite confidence and bounded nonnegative integer input/output usage. A non-unknown choice is admitted only when it is the unique highest-probability choice and meets all three user-selected thresholds. Otherwise the result is unknown. An admitted violated choice maps to work-needed; an admitted satisfied choice maps to satisfied. There is no claim that these illustrative thresholds are calibrated, that probability is a truth estimate, or that unknown means repair work is safe.

Artifact assessment, operational evidence and enforced capability restrictions are separate. Missing operation traces cannot be supplied by an artifact hash. The model may fail to follow a question asking it to recognize that gap. This single-question adapter does not implement atomic obligation decomposition or establish full-contract reliability.

## Optional receipts and privacy

Each call writes at most one fresh mode-0600 receipt when a directory was explicitly supplied. It records schema/versioned model, endpoint, config/question/input/request/response digests when available, scope and decision policy, whether HTTP was attempted, timestamps, raw returned choice/probabilities/confidence and usage for a validated response, final judgment and a fixed error marker. Receipt size is capped at 256 KiB. The configured key value is redacted from string fields as defense in depth. No headers, raw prompts, source contents, response error bodies or environment values are retained. Hashes are not anonymization for guessable content.

An optional receipt is neither a complete replay bundle nor a cost ledger. The caller must retain authorized source snapshots and versioned adapter code separately when reproducibility requires them. Failure to persist the requested receipt fails the process even if the provider already charged. Abrupt termination can leave a partial receipt, and local fsync is not a universal power-loss guarantee. Nothing is uploaded except the explicitly selected provider request; receipts are never published automatically.

## Offline verification

From the repository root:

```sh
bun test experiments/weave-seed/providers/jev.test.mjs
```

Tests inject a mock fetch and a fake credential, and use the no-call gap path for subprocess checks. They cover strict JSON, byte/source/config binding, all choice mappings, abstention, response/usage failures, timeout and body bounds, expiry, private receipts and exact stdout. They make no provider calls. Bun 1.3.5 passed the nine test groups during this implementation; live TypeSafe compatibility and semantic reliability were not tested.

The request shape is based on the retained September 17, 2026 TypeSafe model documentation and the IMP-017 adapter's recorded `jev-1.13.0` request/response schema. That is historical protocol evidence, not a promise that a model remains available. A separately authorized readiness check is required before live use. No lab files or machine-specific paths are imported at runtime.

For acting, the existing process capability accepts a separate explicit executable/argument array. A caller may wrap the existing Prose CLI with a reviewed program and selected native harness, credentials, permissions and private log destination. Such a wrapper must consume the attempt identifier and preserve uncertain effects. This provider directory does not supply an agent loop or claim that native completion proves fulfillment.
