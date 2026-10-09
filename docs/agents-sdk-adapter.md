# Agents SDK adapter

## Packaged setup

CLI release packages include `prose-agents-sdk`, Python 3.10.20, the pinned Agents SDK and HTTPS certificates. Standalone archives contain the helper beside `prose`; npm platform packages contain both in `bin/`; Homebrew installs both executables. Install the complete package and retain the helper with its CLI. Normal use needs no Python installation, source checkout, virtual environment or PATH provisioning.

Both runners select `agents-sdk` by default, with `gpt-6.1-sol`, the `openai-api-key` profile and native output. Supply `OPENAI_API_KEY` through your trusted process environment. Do not put it in command arguments, configuration files or copied support reports. The SDK has no cached-login route and does not switch to another account after an authentication failure.

User choices in `~/.prose/cli.toml` and explicit command overrides take precedence over built-in defaults. An existing saved harness selection remains selected. Use `prose config explain` to inspect the effective choices and their origins, and `prose harness list` to inspect supported adapters. Explicit selection remains available as `--harness agents-sdk --model gpt-6.1-sol --auth-profile openai-api-key --output-contract native`.

The default helper is resolved beside the canonical installed CLI executable. An unrelated `prose-agents-sdk` on PATH does not replace the packaged helper. A missing helper means the installation is incomplete: reinstall the complete matching CLI package. Do not repair it by downloading an unbound executable or provisioning a source launcher.

## Verify before a provider run

Use the installed CLI's provider-free doctor and dry-run routes with the working directory and bounds planned for the action. Readiness reports can establish local selection and availability; key presence does not establish successful authentication or model availability. Dry runs do not invoke a model. See [SDK execution budgets](sdk-budgets.md) for inner and outer deadlines.

For a standalone installation, local helper checks are available using its exact installed path:

```sh
env -i /absolute/installation/prose-agents-sdk --version
env -i /absolute/installation/prose-agents-sdk --packaged-self-test
env -i /absolute/installation/prose-agents-sdk --packaged-tool-self-test
```

Version output is `prose-agents-sdk 0.1.0`. The packaged self-tests check imports, HTTPS certificates, local shell effects, bounded output, cancellation and mocked retrieval. They load no credentials and make no model or network requests. These checks do not qualify a provider account or prove a program's fulfillment.

## Development and specialized integrations

Development from a full CLI source checkout is separate from the installed release route. Use the pinned Python and hash-locked build graph in `harnesses/agents-sdk/requirements-build.txt` through the [packaged SDK build](packaged-sdk-design.md). The release build records the source and dependency hashes, helper identity and notices in package evidence. A developer or test launcher requires an explicitly selected supported override; PATH discovery is not the consumer default.

The weave native actor requires a matching fixed-image CLI with test seams disabled, rather than the ordinary published-kernel loader. Follow [fixed-image staging](../experiments/weave-seed/getting-started/FIXED-IMAGE.md) and [native actor setup](../experiments/weave-seed/integration/native-actor/README.md#explicit-setup). The bridge, coordinator and actor each filter environment names: admit the selected API credential at every required layer, and admit the separate assessor credential where selected. An offline weave check does not establish native provider authentication.

## Effects, limits and evidence

The harness receives opaque instructions, a task prompt and a working directory. It offers a bounded shell for local file operations, public retrieval/search tools and fresh child contexts. Neither the adapter nor harness interprets Contracts or OpenProse syntax or assesses fulfillment.

The shell receives a scrubbed environment without credential-like variables, but can read host files allowed by the operating system. Environment filtering is not filesystem confinement or an OS sandbox. Native events are `start`, `tool_call`, `tool_result`, `final` or `error`. Only `final` settles a successful native run; final text and exit zero do not establish program fulfillment.

Shared budgets account for parent and child calls, tools, usage and cancellation. Turn and token limits are not an aggregate dollar cap. The SDK's automatic model and HTTP retries are disabled; callers own retry allocation. A model can still perform prohibited actions through available tools, and artifact-only assessment cannot certify procedural compliance.

The frozen helper extracts its runtime into temporary storage at startup. It needs a writable temporary filesystem that permits execution. Startup interruption or forced death can leave extraction directories; do not assume cleanup after every signal. Native platform and installed-route qualification, retained package evidence and live program assessment remain distinct from these local self-tests.
