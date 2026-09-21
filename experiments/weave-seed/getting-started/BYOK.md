# Select your own provider and program

This is an explicit local configuration guide, not a live test or a promise that a classifier establishes fulfillment. No OpenProse account is required for the local sidecar. Provider credentials, authorization to send the selected data, spending limits and installed native tools remain your responsibility. The offline example uses none of these.

Contracts define outcomes to achieve and conditions to maintain, together with the invocation's required reviews, reports and constraints. Assessor and actor are provider-neutral roles; the same agent, ordinary code or other authorized participants can supply them through the generic [SDK](../SDK.md), subject to the selected agreement's independence requirements. This configuration helper supports the narrower Jev assessor and Agents SDK/OpenAI-key actor profile described below. That profile does not establish support for other providers. Select evidence for the complete invocation: an already-correct artifact does not discharge a required new review or report.

## Generate the linked configuration files offline

Copy [setup.example.json](setup.example.json) to a separate reviewed input file and replace every placeholder with your own explicit selection. Then run:

```sh
bun --no-env-file experiments/weave-seed/getting-started/configure.mjs /absolute/reviewed-setup.json
```

This helper only materializes configuration. It does not run native readiness, contact a provider, read credential files or modify the selected source files. It checks the selected CLI's bytes against your supplied digest, but does not execute the CLI. The exact expected image identity must already come from your reviewed CLI/build provenance; the helper does not infer it or promise that it matches the kernel until actual native readiness/execution checks occur.

Required inputs are an existing canonical absolute source root, a new single-directory name inside that root, explicit relative kernel/task/contracts/evidence files, absolute Bun and native CLI paths, native executable and expected image digests, an explicit action model, actor environment variable names, an explicit Jev endpoint/model/key-variable name, a reviewed question file, a named decision policy and cumulative `maxAttempts`. This setup profile accepts `maxAttempts` from 0 through 100; a zero-action profile may still assess. The task must be listed among contracts. Sources must be existing regular UTF-8 files with canonical paths inside the root; this helper refuses symlinked source aliases. It does not resolve or automatically adopt dependencies.

`questionFile` names a JSON question conforming to the provider format. You can review and copy [the experimental example](../providers/jev.question.example.json), but its inclusion here is not evidence of general reliability. Endpoint and model selection are explicit. Unknown input fields are rejected, including fields purporting to supply credential values. Only environment variable names belong in the setup manifest. Do not insert a secret into another string field.

The helper creates a new mode-0700 directory with mode-0600 `question.json`, `jev.json`, `actor.json` and `config.json`. The outer config selects all four generated files as evidence, including itself; file paths are references, so this introduces no digest self-reference. It validates the actual provider and actor configuration schemas and source bindings before printing one JSON record with `check`, `status`, `step` and `serve` argument arrays. Existing destinations are never overwritten. A failed setup removes only its newly claimed directory, preserving the original sources. Treat a hard-killed partial directory as an incomplete setup and choose a fresh destination after inspecting it.

The generated profile uses Agents SDK/OpenAI API-key actions, a 20-second Jev deadline, 45-second native readiness and 165-second native process deadlines, a 270-second outer capability deadline, and 300-second observation validity. The printed serve command has **one** maximum step. Optional provider/native receipts are off; enable them only through an explicitly reviewed private-directory configuration change. The first `check` can report missing credential names without using their values. Configuration generation does not grant permission or budget to run the printed step/serve commands.

The helper records a capability version derived from the setup, Bun bytes and a fixed 13-file list covering the setup helper, provider/native adapters, integration config/binding/process helpers, local coordinator/check/entry point, and Bun core/host at setup time. It does not freeze every transitive runtime dependency. Keep the reviewed installation immutable, and regenerate into a new directory after reviewing changes. A new configuration directory is not a way to discard unresolved effects or bypass the previously authorized attempt/spending budget.

Failures exit 1 with one JSON stderr record containing `error`, `stage` and a static `nextStep`. Stages distinguish malformed manifests, source roots, source selection, the Bun executable, the native CLI/digest, provider question/configuration, destination conflicts and generated bindings. No raw exception, selected value or private source text appears in the diagnostic. A successful output explicitly reports `runtimeVersionVerified: false`; the selected Bun file is hashed and checked for executable access, not launched for a version check.

The remaining sections explain each selection and its limits; hand-authoring the same files remains supported.

## Prepare the selected agreement

Create a separate private working directory. Supply an existing verified OpenProse kernel and the actual program you want executed, with every adopted definition required by that program. Preserve their source/version provenance. The synthetic `kernel.md` and `program.md` from the offline example are not substitutes for those files.

List the kernel once in `kernel`, and the program plus adopted definitions in `contracts`. List the source state, required output artifacts and any operation evidence in `evidence`. Paths resolve from the configured source root and must remain within it. An unreadable or missing selected file yields an evidence gap; prepare permitted empty output files only when the agreement allows that initial state. A file-role label, digest or completion report does not independently establish source authority or prove an operation occurred.

The binder does not discover Markdown dependencies or adopted conventions. Installation is not adoption. Select exact files and review the effective agreement before execution. Outputs outside the selected evidence cannot be assessed merely because an actor claims they exist.

## Configure Jev explicitly

Copy the [provider configuration example](../providers/jev.config.example.json) and [question example](../providers/jev.question.example.json) into the source root. Review the endpoint, exact model version, question, scope, explicit thresholds, bounds and optional receipt directory. These examples are unvalidated semantic policy. A provider readiness check would be a separately authorized live action.

Include both files in `evidence`, using the same exact bytes and resolved paths that the provider adapter will read. The adapter rejects missing or stale bindings. Keep them out of `contracts`: these JSON files are assessment configuration data, not automatically adopted agreement. Their contents are sent to Jev, so config must contain only the credential environment variable's **name**, never its value. Configuration and question changes must invalidate cached judgments.

The assessor argv is an array, with absolute executable/script/config paths:

```json
[
  "/absolute/path/to/bun",
  "--no-env-file",
  "/absolute/checkout/experiments/weave-seed/providers/jev.mjs",
  "--config",
  "/absolute/source-root/jev.config.json"
]
```

Select `TYPESAFE_API_KEY` in the coordinator's `environmentKeys` only if that is the configured `apiKeyEnv`. Make its value available through your trusted process environment; do not put it in source files, examples, shell history, receipts or a support report. The process wrapper passes only selected environment variables. Use Bun's `--no-env-file` on coordinator and capability commands to avoid automatic dotenv loading. The adapter itself never discovers credential files.

Keep the parent process timeout greater than the provider timeout. The example provider timeout is 20 seconds; a parent bound must leave time for validation and optional local receipt writes. Byte and returned token limits do not establish a dollar spending cap. The adapter makes one request and does not retry, but repeated coordinator steps can cause additional assessments, including after freshness expiry. Admit a finite number of steps and reserve provider budget before live use. Unknown or failed usage cannot be treated as zero cost.

Read the [provider guide](../providers/README.md) for the exact response policy and private receipt contents. Missing or expired evidence returns unknown without a call. Transport, usage and malformed-response failures exit nonzero. Admitted `violated` maps to `work-needed`; lower-confidence results may map to unknown. The supplied thresholds are not calibrated guarantees.

## Select the action capability separately

The actor is a separate explicit process capability, not part of the Jev adapter. Use a reviewed native-CLI wrapper that consumes the current evidence and attempt identifier, selects the program, passes the exact kernel/contract context, enforces the intended permissions, and retains bounded native evidence privately. An arbitrary CLI command that ignores stdin is not by itself a correct wrapper.

The local [native actor](../integration/native-actor/README.md), with its [configuration example](../integration/native-actor/config.example.json), uses this argument shape:

```json
[
  "/absolute/path/to/bun",
  "--no-env-file",
  "/absolute/checkout/experiments/weave-seed/integration/native-actor/run.mjs",
  "--config",
  "/absolute/source-root/native-actor.json"
]
```

Its first profile selects the existing `agents-sdk` harness and `openai-api-key` authentication. The actor config names an absolute existing CLI executable and its SHA-256, the working directory, kernel and task files, expected image SHA-256, explicit model, selected environment variable names and bounded deadlines. Include that config in evidence too. The kernel must be the bound kernel source and the task must be a selected contract. The wrapper runs native readiness followed by the existing CLI; it does not implement a new agent loop.

Private review bundles contain the sidecars and adapters, not an installed Prose CLI or Agents SDK harness. Obtain those separately through the [native actor prerequisite links](../integration/native-actor/README.md#explicit-setup); the external build instructions require a full CLI checkout.

The existing CLI does not accept an arbitrary runtime kernel override. Its selected image must match the bound kernel and configured image digest. This actor requires a caller-selected fixed-image build for the frozen kernel. It rejects published-on-run builds even if their currently resolved kernel matches. A mismatch fails; changing a JSON path does not change the CLI image. This is a current setup limitation, not an automatic package-resolution feature.

Set the coordinator's outer process timeout above the actor's readiness plus process deadlines, with time for verification and cleanup. An outer timeout can interrupt the wrapper after effects and leave pending state. Keep each bound finite. Follow the selected actor's readiness and configuration documentation. Record the exact Prose CLI, native harness, model, permission profile, selected program and wrapper version. Select only the environment variables that actor requires, in addition to the assessor key. Do not silently substitute a model or login route. Native completion, a zero exit code or a created output file does not prove the contract was fulfilled; the host observes and assesses again afterward.

A shell-capable harness and a fixed file-tool harness have different authority and evidence coverage. Do not describe a trusted wrapper as an OS sandbox. Required/prohibited operations need suitable traces or host enforcement; artifact-only evidence cannot certify operational compliance. Stop and review pending attempts after actor failure, even if an output looks correct.

## Review the coordinator before the first live step

Use the existing [integration configuration](../integration/README.md) and [local coordinator](../local/README.md). Set the source root, exact agreement/evidence selections, explicit assessor and actor argv, allowed environment variable names, private checkpoint directory, cumulative `maxAttempts`, evidence TTL and process timeout. Never place literal environment values in config. A changed executable or transitive dependency needs a changed `capabilityVersion` or another explicit immutable binding; hashing argv does not hash arbitrary executable contents.

Start with offline `check`, then read-only `status`, review the full configuration and data selected for transmission, and authorize one bounded `step` only when ready. Add a bounded `serve` after inspecting that result and the private receipts. This guide has not performed those provider calls. Keep raw state and native traces private; publish only an explicitly reviewed projection. If requesting help, supply sanitized configuration structure, version identifiers and error/status codes rather than keys, private inputs or complete prompts.

General semantic reliability, unseen-contract qualification, cross-platform installation and a hosted account workflow are separate release gates. The local loop can preserve decisions and uncertainty without guaranteeing that the model's decisions are correct.


The native actor requires a fixed-image Prose CLI build. Use the [fixed-image staging guide](FIXED-IMAGE.md) to generate its manifest and hashes without editing them by hand. Before its readiness check it runs `cli doctor --json` and rejects a moving `published-on-run` source, test seams or an unexpected image digest. This prevents an action from starting with a kernel that changes between readiness and execution. The generator's outer process bound is 270 seconds, covering the image-policy and readiness checks (up to 45 seconds each) and the native process bound (165 seconds). Provider and native timeouts remain separate. This is a local build/profile restriction, not a new runtime kernel override or a semantic release qualification.

## Run the configured subject through the CLI bridge

After configuration, select the generated `config.json`, an installed coordinator,
and the fixed-image Prose CLI. Generate a separate host binding; the bridge and
coordinator each have an explicit environment allowlist. For the example setup
above, both layers need `PATH`, `OPENAI_API_KEY` and `TYPESAFE_API_KEY`. If you
changed the configured variable names, use those names instead. Values stay in
your environment and do not belong in either JSON file.

```sh
bun --no-env-file /absolute/installed/source/experiments/weave-seed/getting-started/host-binding.mjs \
  --host /absolute/installed/bin/weave-rust \
  --config /absolute/subject/weave-local-config/config.json \
  --output /absolute/subject/weave-host-binding.json \
  --environment-keys '["PATH","OPENAI_API_KEY","TYPESAFE_API_KEY"]' \
  --timeout-ms 300000 \
  --max-output-bytes 1048576 \
  --prose /absolute/fixed-image/prose
```

Select `weave-bun` instead to use the Bun coordinator. The 300-second bridge
bound exceeds this generated profile's 270-second capability bound. It is a
bound for one invocation, not a promise of completion or a dollar limit.
The helper does not execute the host or contact a provider. Review its printed
commands and run `check` first. A successful check validates local configuration;
it does not establish provider authentication or native harness readiness.
The printed `step` and `serve` commands can contact both providers and cause the
program's permitted effects. The generated serve command remains bounded to one
step. Follow the recovery guide if an action becomes pending.
