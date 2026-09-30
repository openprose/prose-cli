# OpenProse CLI lab

Two independent outer runners, Rust and Bun (packaged through npm), connect an opaque Markdown-owned image and task to an existing agent harness. They do not interpret Contracts or implement the OpenProse language. Keep the kernel, standard library, and component definitions in the separate Markdown library.

## Choose the image and output contract

Ordinary compiled Bun and Rust builds resolve and verify the published kernel before launching an installed harness, append it to native instructions, and keep the task separate. See [kernel startup](docs/kernel-startup.md) for integrity checks, limits and provider-readiness qualifications. Explicit verified-image builds remain available for frozen selections and hermetic fixtures; see [image bundle configuration](cli/shared/image/bundle/README.md).

Both runners support:

- `--output-contract image-envelope` (explicit-image build default): native completion plus the image-declared model-authored terminal envelope.
- `--output-contract native`: actual native completion and final text, without requiring or synthesizing a terminal JSON envelope. Semantic status remains `not-applicable`; evaluate the program's artifacts separately.
- `--output human|json|jsonl`: rendering, independent of those completion rules.
- `--native-log /absolute/new/file.jsonl`: optional private, bounded native-event capture for that same run. It does not prove fulfillment; see [capture limits](docs/native-capture.md).

For example, after an ordinary build and supplying the provider credential in the process environment:

```sh
/path/to/prose --harness claude --auth-profile anthropic-api-key \
  --model haiku --permission-mode acceptEdits \
  --cwd /absolute/language-workspace --output-contract native \
  --output jsonl --native-log /absolute/new-run/native.jsonl run program.md
```

The native-log parent directory must already exist. The image determines how it loads the requested program. Default harness selection is still `openprose`, which reports `HOSTED_UNAVAILABLE`; select an installed harness explicitly. No fallback occurs.

## Harnesses and environment profiles

| Harness | Current routes and capabilities |
|---|---|
| Claude | Installed login or explicit `anthropic-api-key`. The API profile adds native `--bare`, which in 2.1.243 restricts tools to Bash/Edit/Read even with an Agent tool request. Native `acceptEdits` is an explicit permission choice, not implied by API auth. |
| Codex | Installed login or `openai-api-key` through a native custom Responses provider. Explicit `workspace-write` or `read-only` maps to native sandbox selection. Context/skill discovery is a separate concern. |
| Prime / OMP | Explicit `provider/model` and credential profile. Provider-key routes use fresh private configuration; separate harness-login routes preserve native stores. Native tool use is supported. OMP validates its discovered tool inventory rather than requiring it empty. |
| Agents SDK | Optional generic `prose-agents-sdk` executable, explicit model and `openai-api-key`, ordinary shell tool, no built-in delegation or cached-login route. See [installation and limits](docs/agents-sdk-adapter.md). |

Exact admitted versions and platforms are checked at readiness; see the product docs and `cli harness list`. Selected auth is not proof of successful authentication, billing identity, or sufficient capabilities. See [credential routes](docs/api-credentials.md), [permissions](docs/permissions-and-native-notices.md), and [isolated evaluation](docs/isolated-evaluation.md).

Native Claude delegation is available through the opt-in `claude-workspace-tools` native profile. It uses nonbare mode with explicit tools, separate caller-supplied permission rules and directory access, and fresh native configuration for API authentication. The default API profile retains its bare/tool coupling. Native task progress and completion are transported without interpreting their purpose.

## OpenProse service

`prose cli` is also a user client for the hosted OpenProse service: sign in with `prose cli auth login` (or set `OPENPROSE_API_KEY`), then quote, submit, watch and download hosted runs, and manage programs, results, jobs, the wallet and organizations. Public builds talk only to the production service. Start with `prose cli service triage --json`; see [the service client guide](docs/hosted-service-client.md).

## Development and provenance

[Build Rust](cli/rust/README.md) · [Build Bun](cli/bun/README.md) · [Native output semantics](docs/native-output.md) · [Native task events](docs/native-task-events.md)

The initial import preserved the predecessor's current dirty `cli/` tree. `provenance/import.json` records imported hashes and predecessor HEAD; `provenance/source-status.txt` records the working-tree state. This is not a claim that the import equals that commit. Private development binaries, runtime compatibility evidence, and language conformance are separate artifacts; no 1.0 or public-release claim follows from transport success.

The optional [native workspace profile](docs/native-profiles.md) exposes Claude’s ordinary workspace tools, including native delegation, with separate explicit directory access and tool permission rules. Existing defaults remain unchanged.

Generic SDK budgets and their separate inner/outer deadlines are documented in [SDK execution budgets](docs/sdk-budgets.md).
