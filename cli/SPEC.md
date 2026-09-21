# OpenProse CLI Runners

> **Status:** implementation specification
>
> **Branch:** `codex/openprose-cli-vm`
>
> **Repository:** `https://github.com/openprose/prose`
>
> **Scope root:** `cli/`
>
> **Products:** Rust binary and Bun-authored, npm-distributed CLI
>
> **Public command:** `prose`

## 1. Purpose

Build one common shell surface for running the OpenProse language on multiple
agent harnesses:

```text
prose init ...
prose compose ...
prose write ...
prose compile ...
prose run ...
```

The same OpenProse program and the same versioned language instructions must be
portable across harnesses, models, tools, and subagent implementations. The two
CLI implementations are a deliberate bake-off: they expose the same product
surface and run the same conformance corpus, while allowing their native
runtimes and integration techniques to differ.

The three optimization authorities are:

1. developer and agent experience;
2. benchmark quality and trust;
3. portability of OpenProse programs.

These are separate scorecards. A gain in one does not silently excuse a
regression in another.

## 2. Authority and relationship to the language repository

This specification governs the outer `prose` shell runners only. It does not
amend the OpenProse language, skill, standard library, compiler, VM semantics,
or the ongoing deterministic-kernel work elsewhere in the repository.

The governing boundary is the constitutional rule that the complete harnessed
model session is the semantic machine and conventional code protects the
envelope. The runners may preserve bytes, validate closed transport schemas,
enforce lifecycle and security rules, and record observations. They must not
decide what prose means.

Where the earlier `OPENPROSE.cli.md` brief assigns semantic context selection,
compilation, frame construction, child settlement, Return validation, or
canonical OpenProse evidence to the CLI, this specification supersedes that
brief. Where `OPENPROSE.contract-vm-architecture.md` describes a future
deterministic runtime kernel, that kernel is a separate possible VM
implementation, not a responsibility of these wrappers.

The existing skill and its underlying Markdown remain source material for the
language team. CLI implementation agents must not modify them.

## 3. Non-negotiable product model

### 3.1 Two independent entry paths

OpenProse has two independent ways to enter the same language.

#### Direct skill/TUI path

1. The user starts Claude Code, Codex, Prime Agent, OMP, or another supported
   harness normally.
2. The `open-prose` skill is installed in that harness.
3. The user types or prompts `prose ...` inside the harness.
4. The skill activates and the harnessed model embodies the OpenProse VM using
   its native tools and subagents.
5. No OpenProse shell CLI participates.

The skill's recursion rule is normative in this path: the embodied VM must not
shell out to a `prose` executable or `npx prose`.

#### Wrapper path

1. The user invokes the `prose` executable from a shell.
2. The runner resolves a configured launch adapter.
3. It launches or enters that adapter noninteractively.
4. It injects the versioned, skill-owned Skill Runtime Image.
5. It forwards the original OpenProse command as an exact model-facing task
   envelope.
6. It streams the harness result and supervises transport lifecycle.
7. The top-level model, its harness tools, and any native subagents together
   embody the VM implementation.

The wrapper does not open, embed, automate, or emulate a terminal TUI. The two
entry paths never call one another.

### 3.2 One virtual machine surface

The portable relation is:

```text
Skill Runtime Image + task envelope
                     │
                     ▼
               launch adapter
                     │
                     ▼
        harnessed semantic machine
   (root model + tools + native subagents)
                     │
                     ├── reads the OpenProse program from the authorized cwd
                     ▼
       observable results and VM-owned evidence
```

The launch adapter is an ABI shim. It transports the language image, command,
events, cancellation, and mechanical metadata. It is not an OpenProse
interpreter.

### 3.3 Terminology

- **Skill Runtime Image:** a versioned Markdown release artifact, owned and
  produced by the OpenProse skill/language layer, that can boot a fresh
  noninteractive harness session without ambient skill discovery. It is not
  the compiled capital-`Image` used by other OpenProse architecture.
- **Task envelope:** the language-owned, model-facing, shell-neutral
  representation of one `prose ...` command.
- **Runner invocation record:** the internal transport/lifecycle record that
  surrounds a task envelope. Runner metadata in this record is not model
  context.
- **Launch adapter:** the outer process, protocol, SDK, embedded, or hosted
  integration that starts one harnessed VM run.
- **In-session host mapping:** the skill's mapping from OpenProse's abstract
  host primitives to native tools and subagents. In this specification,
  **adapter** means only the outer launch adapter; the in-session mapping
  remains language/VM behavior and is outside the runner API.
- **Transport completion:** the top-level harness invocation reached a
  documented terminal state.
- **Semantic completion:** the embodied OpenProse VM reports completion under
  a skill-owned terminal contract. These are not interchangeable.
- **Prose Complete:** an empirical conformance claim about a VM
  implementation, never a claim inferred from an SDK or feature list.

## 4. Explicit non-goals

The runners do not:

- parse Contract Markdown or ProseScript;
- select language documentation based on command meaning;
- resolve OpenProse dependencies;
- compile OpenProse source;
- construct OpenProse frames or bindings;
- spawn or settle OpenProse child frames from the outer process;
- validate declared Returns or postconditions;
- infer semantic success from assistant prose;
- record canonical language evidence independently of the VM;
- silently translate or repair OpenProse programs for a harness;
- open a harness TUI or scrape ANSI output;
- install third-party harnesses or copy their credentials;
- auto-install the direct-TUI skill into a harness;
- silently select a different adapter, authentication route, or billing owner;
- require a daemon for ordinary local commands; or
- modify the current language/kernel implementation outside `cli/`.

An embedded adapter may provide generic model, tool, filesystem, terminal, and
subagent primitives to its agent runtime. Providing those host capabilities is
not permission to encode OpenProse semantics in the adapter.

## 5. Repository layout and isolation

All implementation work lives in one new, independently buildable subtree:

```text
cli/
  SPEC.md
  README.md
  AGENTS.md

  shared/
    schemas/
    fixtures/
    image/
    capabilities/
    errors/

  conformance/
    runner/
    fake-harness/
    cases/
    adversarial/

  benchmarks/
    runner/
    programs/
    analysis/
    policy/

  rust/
    Cargo.toml
    Cargo.lock
    crates/
      prose-cli/
      prose-runner-core/
      prose-process-supervisor/
      prose-adapter-*/

  bun/
    package.json
    bun.lock
    src/
      core/
      supervision/
      adapters/
    npm/
      meta/
      platform-template/

  protocol/
    STATUS.md
    OWNERSHIP.md
    tasks/
    decisions/

  ci/
  release/
```

Work occurs in one clean worktree created from `openprose/prose` `main`. The
existing checkout containing ongoing language work remains untouched. The
worktree and branch are shared by the implementation swarm; path leases, not
additional worktrees, provide isolation.

The Rust tree is a standalone Cargo workspace and the Bun tree is a standalone
package/workspace. During the bake-off they are not added to the repository's
root Cargo or pnpm workspaces. This avoids touching the active language layer's
`Cargo.toml`, `Cargo.lock`, `package.json`, `pnpm-lock.yaml`, or `crates/op-cli`.

GitHub only recognizes workflow files under repository-root
`.github/workflows/`. The implementation now includes uniquely named,
path-scoped CLI CI and manual draft-only release workflows there. They remain
under the CLI integration boundary and do not publish, tag, or promote bytes;
all substantive scripts and configuration remain in `cli/ci/`.

## 6. Skill Runtime Image contract

### 6.1 Ownership

The language/skill layer owns:

- semantic prompt text;
- which Markdown documents are included;
- ordering and progressive-disclosure policy;
- child-context instructions;
- host-primitive semantics;
- the semantic terminal envelope; and
- the relationship between the direct skill and wrapper image.

The CLI project owns only the deterministic packaging and transport contract.
It consumes a language-team artifact; it does not hand-author a replacement.

### 6.2 Required artifact

A release image contains at least:

```text
image-format-version
language-version
skill-version
runtime-contract-version
semantic-source-revision
ordered payload entries
per-entry SHA-256
aggregate SHA-256
ordered permitted instruction placements and strictness
language-owned task-envelope and one-field framing identifiers
terminal-envelope schema
minimum transport requirements
```

Canonicalization rules are UTF-8, LF newlines, no byte-order mark, stable entry
ordering, and byte-for-byte reproducibility. Rust and Bun release artifacts
must embed the same logical image and aggregate digest.

The runner may inspect and validate envelope metadata, lengths, schema
versions, and hashes. It treats Markdown bodies as opaque bytes.

### 6.3 Image and invocation delivery

The Skill Runtime Image and model-facing task envelope are separate transport
values. The image manifest declares an ordered, closed set of permitted
instruction placements and whether each preserves strict conformance. The
launch adapter maps those entries to documented harness mechanisms, selects
the first mechanically supported placement, delivers the image unchanged
there, and delivers the task envelope through the user-task channel. It must
not invent a notion of the "strongest" channel or interpolate into, delete
from, reorder, rewrite, or branch on the image body. If no permitted placement
exists, it fails with `PROMPT_CHANNEL_UNSUPPORTED`; a degraded fallback is
legal only when the manifest explicitly permits it.

A transport that exposes only one text input may combine the values only with
a language-owned, versioned framing template identified by the image manifest.
The operation is mechanical and preserves both values; adapter code does not
invent prose around them. Evidence records the two source digests and the
rendered-payload digest separately.

Version 1 has one Skill Runtime Image for every forwarded language command.
Command-specific capsules are not part of this implementation. Introducing
them later requires a new image-format version and a separately ratified
boundary demonstrating that selection does not move semantic routing into the
runner.

### 6.4 Current source conflict

The present `guidance/system-prompt.md` is not a releasable wrapper image: it
contains retired service/system/Ensures terminology and routing that conflicts
with the current `SKILL.md`. Concatenating the current documentation would also
retain filesystem-loading assumptions and duplicate guidance.

Producing a coherent Skill Runtime Image is therefore a language-team release
gate. CLI transport work proceeds against the nonsemantic `echo-v0` functional-
alpha image and the separate sentinel/test fixtures until the canonical
artifact is supplied. CLI agents must not resolve the conflict by editing or
silently rewriting language documents.

### 6.5 Isolation and precedence

By explicit project policy, wrapper runs suppress ambient user/project skills,
memories, plugins, MCP servers, instruction files, and auto-discovered
OpenProse installations where the harness permits it. This is a transport
isolation rule, not an inference about program meaning. They preserve:

- the harness's native tool and safety prompt;
- managed organization policy;
- the selected permission/sandbox policy; and
- the harness's own authentication mechanism.

The Skill Runtime Image is placed only through the ordered manifest rule in
§6.3. Replacing a harness's complete native prompt is avoided unless the
manifest permits it and that adapter has a specific, tested mechanism.

Every adapter has a prompt-precedence canary. An adapter that can deliver only
an ordinary user prefix is marked degraded and cannot claim strict wrapper
conformance until the language image explicitly permits that fallback.

Version 1 normal language execution selects only a strict-wrapper-conformant
adapter; there is no `--allow-degraded` execution mode. Degraded adapters may
be probed and exercised in explicitly named research tests, but an ordinary
run fails with the relevant capability error rather than weakening isolation,
prompt placement, billing identity, lifecycle, or terminal-envelope guarantees.

## 7. CLI surface

### 7.1 Forwarded language commands

After runner-global options are consumed, all remaining arguments are opaque
OpenProse command-language input. Current examples include:

```text
init compose write compile serve run lint preflight test inspect status
install upgrade help examples
```

Unknown future commands follow the same path without a runner release.
`prose help` is language help and is forwarded; `prose --help` describes the
shell runner.

The bare first token `cli` is the one permanent operational reservation.
`prose -- cli ...` strips the delimiter and forces `cli ...` through the
language path, preserving a deliberate escape hatch.

The forwarded `argv` is the literal `prose` introducer followed by the
remaining argument vector after runner-global options; no display string or
shell reconstruction is authoritative.

### 7.2 Runner-owned commands

The only reserved command-language noun is `cli`. Runner operations live
under it so a future language command does not collide with runner behavior:

```text
prose cli doctor [--json]
prose cli harness list [--json]
prose cli config explain [--json]
prose cli auth login|logout|status
prose --help
prose --version
```

Official benchmark tooling is an external artifact-level rig under
`cli/benchmarks/`; there is no runner-owned benchmark command in v1.

`prose cli auth` manages only the OpenProse account. Third-party authentication
remains harness-owned; diagnostics print the exact native login command when
one is documented.

Wrapper language runs are always noninteractive. If the embodied VM requires
caller input or approval that the selected transport cannot resolve, it emits
the skill-owned unresolved-input or unresolved-approval terminal status. The
runner never opens a prompt to continue the run. Users who want conversational
questions, approvals, or a TUI use the direct skill path.

### 7.2.1 Proposed local weave host bridge (IMP-026)

This isolated branch proposes `prose cli weave --host-binding ABS OP CONFIG ...`.
It is not a released command. The exact admission, operation grammar, byte-stream,
exit and interruption contract is [the IMP-026 decision](protocol/decisions/imp026-weave-host.md),
with its self-contained [black-box fixture](shared/fixtures/weave-host-v1.json).
Implementation must follow that corpus in both products before qualification.

The bridge invokes an explicitly selected, digest-pinned local coordinator; it
never implements or discovers language `init`, `run`, or other Markdown commands.
`prose weave ...` and `prose -- cli weave ...` remain opaque language invocations.
It bypasses runner configuration, kernel/image resolution and harness discovery.
Its dedicated host binding contains executable identity, selected environment
names and resource bounds, not credentials or harness defaults. No existing
runner-global flag is admitted for this operation, including `--dry-run`.

### 7.3 Global options

The grammar is:

```text
prose [runner-global-options...] <language-command> [opaque-language-args...]
prose [runner-global-options...] -- <opaque-language-argv...>
```

The first non-runner-option token, or `--`, permanently ends global-option
parsing. Every later token is forwarded as an unchanged argument, including a
token named `--harness`, `--model`, or any future runner option. The sole
positional exception is an unescaped first token `cli`, which enters §7.2.
Runner-global options include:

```text
--harness <id>
--transport <id|auto>
--cwd <path>
--model <id>
--timeout <duration>
--output <human|json|jsonl>
--dry-run
--no-color
--verbose
```

This prevents the runner from stealing similarly named language/program
arguments.

`--dry-run` performs configuration discovery, executable/version probing,
image verification, and adapter negotiation but starts no model or agent run.
Its human and machine outputs report the canonical cwd, selected
harness/transport and version, prompt placement, isolation guarantee, auth
category/readiness, billing owner, image version/digest, configuration sources,
and any blocking diagnostic. It never emits secrets or rendered image bodies.

### 7.4 Configuration precedence

The closed precedence is:

1. invocation flags;
2. `PROSE_*` environment overrides;
3. project `.prose/cli.toml`;
4. user platform configuration for OpenProse;
5. built-in defaults.

`--cwd` is applied before project discovery. The directory must exist and is
resolved to a canonical, symlink-resolved absolute path. From there the runner
searches upward for the first `.prose/cli.toml`, including the nearest VCS root
but never crossing it; without a VCS boundary it may search to the filesystem
root. Symlink aliases of the same file are loaded once.

The user configuration file is `$XDG_CONFIG_HOME/openprose/cli.toml` when
`XDG_CONFIG_HOME` is set, otherwise
`~/Library/Application Support/OpenProse/cli.toml` on macOS,
`~/.config/openprose/cli.toml` on Unix, and
`%APPDATA%\OpenProse\cli.toml` on Windows. Tests replace these roots through an
internal dependency, not undocumented production environment variables.

Version 1 consumes only `PROSE_HARNESS`, `PROSE_TRANSPORT`, `PROSE_MODEL`,
`PROSE_TIMEOUT`, `PROSE_OUTPUT`, `PROSE_COLOR`, `PROSE_VERBOSE`, and
`PROSE_AUTH_PROFILE`. Other `PROSE_*` names are not runner configuration.
Unknown configuration-file keys and invalid values fail closed with their
source location; unknown environment variables are ignored by the runner.

Configuration files use a deliberately closed, portable subset of TOML. Each
nonblank physical line contains either one comment or one assignment to a
known top-level bare key. Spaces and tabs may appear around the key, equals
sign, and value. A comment begins with `#` outside a string and continues to
the end of the physical line. Files must be valid UTF-8, may use LF or CRLF
line endings, and do not need a final newline.

Values are the lowercase booleans `true` and `false`, single-line basic
strings, or single-line literal strings. Basic strings admit only `\"`, `\\`,
`\b`, `\t`, `\n`, `\f`, `\r`, `\uXXXX`, and `\UXXXXXXXX`; Unicode escapes must
name a Unicode scalar value. Literal strings have no escape syntax and cannot
contain an apostrophe. Raw control characters are not allowed in either string
form. The `color` and `verbose` keys require booleans; every other key requires
a string.

Tables, dotted or quoted keys, arrays, numbers, dates, multiline strings, line
continuations, unsupported escapes, duplicate keys, multiple assignments on
one line, and all other TOML syntax are rejected. A configuration error names
only the safe error classification and exact `path:line` source. It never
copies the rejected source line or value into human, JSON, or JSONL output.

The default harness is `openprose`. There is no discovery-based fallback.
`prose cli config explain` reports each effective value and its source without
printing secrets.

Installed-harness discovery informs `prose cli doctor` and
`prose cli harness list`. It never changes the selected harness automatically.

`prose cli harness use <id>` writes one atomic user-scoped selection bundle.
For Prime and OMP, the command requires an explicit fully qualified
`--model <provider/model>` and a recipe-declared `--auth-profile <id>`; only
values supplied on that CLI invocation may be persisted. Effective project,
environment, and existing user-config values are not silently promoted into a
new default. Codex and Claude may omit both values and then use only their
separately frozen cached-login/subscription defaults. Every switch removes
stale saved model/profile members unless the user supplies replacements. The
file stores route identifiers, never credential values. Selection performs no
harness discovery, auth probe, or model execution.

### 7.5 Output and exit behavior

- Human output is quiet, streaming, and useful by default.
- JSON emits exactly one final result object followed by one LF on stdout.
- JSONL emits ordered normalized event objects followed by exactly one
  `runner.completed` or `runner.failed` terminal object; no event follows it.
- Machine modes are stable, versioned, and free of terminal decoration.
- stdout carries result/event output; stderr carries runner diagnostics.
- Stable runner errors have codes, boundary names, and one corrective action.
- Raw protocol data is opt-in debug evidence, bounded, and redacted.

Transport completion alone never becomes an OpenProse semantic claim. A
strict-wrapper adapter requires the skill-owned terminal envelope before
reporting semantic success. A transport that cannot carry or recover that
envelope reports `semanticStatus: unknown`, exits nonzero in every normal v1
execution, and is not eligible for a Prose Complete claim. A diagnostic or
explicit research probe may record that status without normal execution. A
runtime image may instead declare a closed, nonsemantic terminal contract with
`semanticStatus: not-applicable`: the transport sentinel and functional-alpha
echo image use that narrow exception. A completed invocation then exits zero
because the declared transport task completed, while making no OpenProse
language or semantic claim. Full release admission still requires a canonical
language runtime and its semantic terminal contract.

All results include a normalized terminal classification:
`success|semantic-failed|cancelled|terminated|timeout|killed|exit-code|runner-error`.
The language-owned terminal contract declares the generic mapping from its
terminal envelope to `success|semantic-failed|unknown`; nonsemantic image
contracts additionally fix `not-applicable`. Runner code performs only that
versioned structural projection and does not infer the mapping from assistant
prose or domain Returns.
Portable runner exit codes are `0` for semantic success or successful
image-declared nonsemantic placeholder completion, `2` for invocation or
configuration errors, `10` for unavailable/incompatible/auth/quota errors,
`20` for image/prompt/transport/recursion rejection, `21` for timeout, `22` for
protocol or harness failure, `23` for unknown semantic status, `24` for
cancellation/termination, `25` for cleanup failure, `30` for a structured
semantic failure, and `70` for an internal runner fault. Platform-native
process exit and signal details remain separate result fields; Unix shells may
observe `130`/`143` only when the wrapper itself is directly terminated before
it can emit its normalized result.

## 8. Shared invocation and result protocols

The runner-transport definitions are JSON Schemas under
`cli/shared/schemas/`. Language-owned task and semantic terminal schemas are
digest-pinned external inputs as specified in §16.2. The following shapes are
illustrative requirements, not a second schema home.

### 8.1 Internal invocation and model-facing task

```json
{
  "schema": "openprose.runner-invocation/1",
  "invocationId": "uuid-or-content-safe-id",
  "cwd": "/canonical/working/directory",
  "languageImage": {
    "version": "...",
    "sha256": "..."
  },
  "runner": {
    "name": "rust|bun",
    "version": "..."
  },
  "harness": "codex",
  "transport": "exec-json",
  "recursionToken": "opaque",
  "task": {
    "schema": "openprose.task-envelope/1",
    "argv": ["prose", "run", "path with spaces/example.prose.md"],
    "interactionMode": "non-interactive"
  }
}
```

The full runner record is never delivered to the model. Only the language-owned
`task` value enters the user-task channel. The v1 task contains the original
`argv`, fixed noninteractive mode, and only any additional field explicitly
declared by that language-owned schema. Runner/harness/transport identity,
billing metadata, IDs, image digests, and recursion markers stay out of band
unless a later image version explicitly adopts a field.

Argument boundaries in `task.argv` are authoritative. Any human-readable
command rendering is diagnostic only and uses a deterministic display escaper.
The runner never reconstructs a shell command. Recursion protection travels
through the supervised process environment or protocol metadata, not model
context.

### 8.2 Mechanical adapter descriptor

A launch adapter reports transport facts, not semantic language claims:

```json
{
  "schema": "openprose.launch-adapter/1",
  "id": "codex/exec-json",
  "runtime": "installed-process",
  "promptPlacement": "developer",
  "preservesHarnessBasePrompt": "enforced",
  "sourceBytesVerified": "enforced",
  "adapterInputContentVerified": "enforced",
  "modelRequestObserved": "unsupported",
  "streaming": "structured",
  "cancellation": "process-tree",
  "terminal": "structured",
  "resume": "enforced",
  "isolation": "managed-policy-preserving",
  "auth": ["harness-managed"],
  "billingOwner": "user-provider",
  "observedBy": ["fake-harness-byte-equality", "prompt-precedence-canary"],
  "platforms": ["darwin-arm64"],
  "versionRange": "..."
}
```

Guarantee fields use closed levels such as `enforced`, `advisory`, and
`unsupported`; they are not loose booleans.

Image exactness means equality of the canonical UTF-8 payload after transport
decoding at the harness instruction boundary, not equality of JSON, RPC, or
provider wire bytes. Release artifacts and adapter request construction retain
the canonical source digest. Where the instruction boundary is observable,
the adapter also records the delivered-content digest. Where it is not, the
descriptor names the documented normalization boundary and relies on a
content/precedence canary without claiming unobservable end-to-end identity.

Semantic facts such as child isolation, parallelism, Return correctness, and
OpenProse frame settlement live in a separate observed VM conformance report.
They are measured by running programs, never inferred from this descriptor.

### 8.3 Normalized events

At minimum:

```text
runner.started
harness.started
assistant.delta
assistant.message
tool.started
tool.updated
tool.completed
approval.requested
usage.observed
diagnostic
harness.completed
runner.cancelled
runner.failed
runner.completed
```

Adapters may retain a namespaced raw payload in debug evidence. User-facing and
benchmark consumers depend only on normalized events and the final result.
Incremental framing applies fixed per-record, aggregate-output, and queue
limits from the shared schema profile. Producers pause or are cancelled when
backpressure cannot be honored; records are never silently dropped.

### 8.4 Result

The runner result records:

- runner implementation/version/commit;
- adapter and detected harness version;
- transport and negotiated mechanical capabilities;
- Skill Runtime Image version and digest;
- internal invocation-record digest, model-facing task digest, and canonical
  working-directory identity;
- start, first-event, cancellation, and terminal timestamps;
- harness terminal/exit/signal classification;
- language terminal-envelope status when present;
- usage and cost only when authoritative, otherwise `unavailable`;
- billing owner and auth mechanism category;
- normalized-event digest; and
- sanitized diagnostic references.

The runner validates the skill-owned terminal envelope structurally. It does
not inspect domain Returns to decide whether they are good.

Production invocation IDs are UUIDv7 values and wall timestamps are UTC
RFC 3339 with monotonic durations. Conformance injects deterministic ID and
clock sources through an internal test interface. Differential comparison
removes only schema-declared volatile fields and compares every other value.

### 8.5 Error taxonomy

Stable categories include:

```text
CONFIG_INVALID
HARNESS_UNAVAILABLE
HARNESS_INCOMPATIBLE
HARNESS_NEEDS_AUTH
TRANSPORT_UNSUPPORTED
PROMPT_CHANNEL_UNSUPPORTED
IMAGE_INVALID
IMAGE_TOO_LARGE
RECURSIVE_INVOCATION
STARTUP_TIMEOUT
PROTOCOL_MALFORMED
PROTOCOL_TRUNCATED
HARNESS_FAILED
SEMANTIC_STATUS_UNKNOWN
CANCELLED
PROCESS_CLEANUP_FAILED
HOSTED_UNAVAILABLE
HOSTED_AUTH_REQUIRED
HOSTED_QUOTA_EXCEEDED
```

The two implementations use the same codes, exit classifications, and
corrective-action text fixtures. The taxonomy maps to the exit-code groups in
§7.5 as follows: `CONFIG_INVALID` → `2`; harness availability,
compatibility/auth, and hosted availability/auth/quota → `10`;
transport/prompt/image/recursion rejection → `20`; `STARTUP_TIMEOUT` → `21`;
protocol and harness failure → `22`; `SEMANTIC_STATUS_UNKNOWN` → `23`;
`CANCELLED` → `24`; and `PROCESS_CLEANUP_FAILED` → `25`.

## 9. Adapter architecture

### 9.1 Runtime classes

Adapters are classified by runtime ownership and transport, not by a false
SDK-versus-subprocess dichotomy:

1. **Installed process:** user-installed executable and harness-owned
   authentication.
2. **SDK-managed process:** a library API that supervises a bundled or local
   executable.
3. **True embedded runtime:** the CLI process owns the agent loop/library.
4. **Hosted runtime:** OpenProse owns the account/billing boundary and some or
   all of the agent execution.
5. **Deterministic mock:** transport and fixture testing only; never a semantic
   fallback.

### 9.2 Required external-process coverage

Prime Agent, OMP, Codex CLI, and Claude Code installed-process adapters are the
mandatory public parity baseline. Rust and Bun expose the same harness and
transport IDs for them. A shared adapter identity has one lead-owned capability
record and one behavior contract; an implementation may report `unavailable`
while incomplete, but it may not weaken that record to excuse drift.

| Harness | Baseline transport | Richer variants | Authentication |
| --- | --- | --- | --- |
| Prime Agent | native RPC (`--mode rpc`) | ACP | harness-managed installed login/API configuration |
| OMP | native RPC (`--mode rpc`) for exact `@oh-my-pi/pi-coding-agent@18.0.9` | ACP | harness-managed installed login/API configuration |
| Codex | `codex exec --json` | app-server; SDK | official Codex cached login or API key |
| Claude Code | `claude -p` streaming JSON | Agent SDK | installed CLI auth for unmodified local CLI |

The baseline is direct executable invocation with structured pipes. Richer
protocols are added when their extra lifecycle behavior is used and tested,
not merely because they exist.

Before work begins on any real adapter, a versioned admission recipe freezes
its executable/package identity, supported version range, noninteractive and
structured-output flags, instruction placement, ambient-isolation controls,
cancellation protocol, auth category, billing owner, and required terminal
event. Strict support is earned by tests against that recipe rather than
assumed from a feature list.

For Claude subscription-backed wrapper runs, isolation uses the mechanism that
retains normal authentication while disabling customizations; bare/API-key
mode is a separate variant. Embedded Claude Agent SDK support is API/cloud
credential based unless Anthropic explicitly approves another product flow.

### 9.3 Bun/npm adapter coverage

Implementation-specific research candidates:

- `codex-sdk` using `@openai/codex-sdk`;
- `claude-agent-sdk` using the Claude Code preset plus the skill image, with
  API/cloud-provider authentication;
- `pi-openrouter` using Pi's coding-agent SDK and OpenRouter credits/API key;
- `omp-embedded` using the Bun-native OMP SDK;
- `openai-agents-sdk` as a distinct API-billed embedded harness experiment;
- Prime's process/RPC integration first, with direct package integration only
  if it preserves Prime-specific behavior.

The Codex SDK and Claude Agent SDK are SDK-managed process adapters, not proof
of process-free execution. OpenAI Agents SDK and Pi are agent-construction
runtimes and must provide a complete generic coding/subagent substrate before
they can be compared as Prose Complete VMs.

Codex SDK and Claude Agent SDK remain experiments until clean-install,
Bun-compatibility, standalone-compilation, bundled-asset, license, and
cancellation tests pass. They need not be included in the base npm artifact
merely to satisfy the installed-process parity baseline.

### 9.4 Rust adapter coverage

- `agentclientprotocol/rust-sdk` is a transport/client implementation for ACP
  agents, not an embedded agent itself. It is used for ACP-capable local or
  remote adapters.
- Rig is a true embedded Rust agent framework. It is an experimental VM
  candidate and a likely engine for an OpenProse-owned runtime, not a shortcut
  to semantic parity.
- Codex app-server and other JSON-RPC/ACP processes remain valid Rust
  integrations without a provider-specific Rust SDK.

SDK, ACP, app-server, Pi, Rig, and embedded variants are reported on their own
research scorecards. They are excluded from the basic Rust-versus-Bun product
winner comparison unless the same user-visible harness/transport contract is
available in both products. This does not prevent implementation-specific VM
research; it prevents feature breadth from masquerading as runner quality.

Implementation-specific research adapters ship only in separately named lab
artifacts or compile-time research profiles. They do not appear in either
default distribution, shared help, `prose cli harness list`, stable
configuration schema, or public compatibility claims. A research adapter may
enter a default product only after the same user-visible identity and behavior
contract exists in both products, or after an explicit post-bake-off decision
relaxes public parity.

### 9.5 Transport selection

User-facing harness IDs remain stable while `--transport auto` chooses a
supported implementation based on an explicit preference table and version
probe. Benchmarks always pin a transport. `auto` is observable through
`prose cli doctor`, dry-run output, and result evidence.

No adapter silently falls back to a different harness, wrapper-selected
credential environment group, billing owner, or semantically weaker prompt
channel. Where a functional-alpha process adapter preserves harness-owned
configuration, cached login, or keychain state, provider selection and fallback
inside that harness are explicitly outside the wrapper's control and must not
be reported as independently verified.

## 10. Process and session supervision

Installed and SDK-managed processes obey one shared supervision contract in
each implementation:

1. Resolve the executable to an absolute real path and probe its version.
2. Reject a path that resolves to the current `prose` wrapper.
3. Spawn directly with an argument array; never use a shell.
4. Set the canonical working directory explicitly.
5. Use separate stdin, stdout, and stderr pipes.
6. Never allocate an outer PTY or permit a TUI mode.
7. Reserve structured stdout/protocol channels from human diagnostics.
8. Incrementally frame bounded JSONL/RPC records with backpressure.
9. Place the child in an owned Unix process group or a race-free Windows Job
   Object. Windows strict mode uses suspended creation, assignment to a Job
   with `KILL_ON_JOB_CLOSE`, then resume, or an equivalently audited native
   launcher.
10. Propagate cancellation through the strongest documented protocol first.
11. After a grace deadline, terminate and then hard-kill only the owned tree.
12. Resolve completion exactly once despite exit/cancel/timeout races.
13. Require both a documented terminal event and acceptable process exit where
    the protocol provides both.
14. Treat EOF without the required terminal event as protocol failure.
15. On catchable runner termination, leave no descendant retained by the
    declared containment mechanism. Attempted detachment makes the run fail
    cleanup conformance and the adapter non-strict.

If a runtime cannot implement the required containment directly, it may use a
small audited platform helper. Otherwise strict process cleanup is declared
unsupported on that platform. SDK-managed adapters may claim only the
cancellation and process ownership their documented API actually exposes.

Cleanup audits use owned process handles, process-group/Job identity, and a
per-run nonce inherited by fixture descendants; they never trust a PID alone.
Tests include grandchildren, cancellation-resistant children, and attempted
session/Job detachment. The adversarial fixture must detect its own attempted
detachment, and a strict adapter may target only harness versions documented
not to detach. No claim covers an unknown external process that escapes both
the containment boundary and audit nonce, uncatchable power loss, or other
unobservable behavior.

Harnesses may create PTYs internally for tools they execute. That is not the
transport between OpenProse and the harness.

Prompt files, when required, are mode `0600`, contain no credentials, use a
private temporary directory, and are removed after the child no longer needs
them.

## 11. Authentication, billing, and environment policy

### 11.1 Principles

- The selected billing owner is shown before execution in dry-run or
  `prose cli doctor` output and recorded in the result.
- No fallback may change who pays.
- Project configuration may name an auth profile but never contain a token.
- The runner never scrapes, imports, copies, or logs third-party auth files.
- Installed harnesses consume their own credential caches and keychains.
- Secrets never appear in argv, structured logs, benchmark artifacts, or error
  messages.

### 11.2 Authentication classes

- `openprose`: OpenProse account; OpenProse billed.
- Codex CLI/SDK: official cached Codex/ChatGPT login or platform API key, as
  reported by the official runtime.
- OpenAI Agents SDK: provider/API credential, not a ChatGPT subscription.
- Installed Claude Code: the unmodified CLI's own user authentication. The
  wrapper does not offer or intermediate Claude.ai login.
- Claude Agent SDK: API key or supported cloud-provider credentials unless
  specifically approved otherwise.
- Pi/OpenRouter: OpenRouter API key or OAuth-created key charged against
  OpenRouter credits.
- Prime/OMP: `harness-managed`; the CLI makes no stronger entitlement claim
  than the harness and provider permit.

### 11.3 Environment isolation

The runner uses an adapter-specific environment policy rather than blind
inheritance. It retains OS variables required for executable discovery,
keychains, locale, temporary directories, and the selected harness's documented
configuration. It strips OpenProse billing tokens from third-party children and
does not pass unrelated provider credentials.

Additional environment names must be explicitly configured. Values are never
shown by `prose cli config explain` or `prose cli doctor`.

The Bun standalone build disables automatic `.env` and `bunfig.toml` loading.

### 11.4 Recursion protection

Every run carries an invocation ID and recursion marker. A nested `prose`
process in the owned process tree fails with `RECURSIVE_INVOCATION`. Resolution
also rejects aliases or symlinks that point the selected harness executable
back to the wrapper.

## 12. OpenProse-billed default

`openprose` is the stable default harness identity in both products. It must:

- use an OpenProse account and explicit billing;
- require no ChatGPT, Claude, OpenRouter, or other provider credential;
- run the same Skill Runtime Image as every other adapter;
- expose the same normalized invocation/event/result contract;
- keep local workspace authority explicit and least-privileged;
- report authoritative usage/quota/billing evidence; and
- never fall back to a BYO adapter.

Until this adapter is implemented and enabled, selecting the default returns
`HOSTED_UNAVAILABLE` with an actionable explanation. It never selects the
mock or an installed third-party harness. Local walking-skeleton tests and BYO
runs therefore select their adapter explicitly during the earlier phases.

The physical placement of its agent loop remains a gated architecture
decision. The shared protocol must support evaluation of both:

- a locally supervised harness/model loop using an OpenProse billing/model
  gateway; and
- an OpenProse-hosted agent loop that requests narrowly scoped local
  filesystem/terminal capabilities through the CLI.

Before the model-backed default is implemented, a decision record must compare
security boundary, source exposure, latency, cancellation, offline behavior,
tool semantics, cross-language parity, evidence authority, operational cost,
and user understanding. Work before that decision uses a scripted hosted
service and local capability bridge fixture. The public `openprose` adapter
contract does not depend on which placement wins.

OpenProse authentication uses OS credential storage for local users and a
scoped environment token for CI. Exact account endpoints, device/browser login,
token scopes, pricing, quotas, spend limits, retention, and regional behavior
are separate product decisions that block public hosted release, not local
adapter development.

## 13. Rust implementation

The Rust deliverable is a small native `prose` binary with:

- fast startup for local runner commands;
- a runtime-neutral core;
- structured process supervision;
- adapter crates behind a common trait;
- stable human and JSON output;
- no dependency on repository language/kernel crates; and
- reproducible release archives for supported platforms.

The core trait is conceptually:

```text
probe(context) -> readiness + descriptor
start(invocation, image, cancellation) -> event stream + terminal result
```

ACP is an optional transport crate. Rig remains isolated behind an experimental
adapter boundary so its release cadence and agent implementation cannot expand
the core runner.

## 14. Bun/npm implementation

The npm deliverable is authored, built, and tested with Bun. It uses the same
schemas and fixtures as Rust and exposes the same `prose` command.

### 14.1 Distribution

The intended npm shape is:

```text
@openprose/prose-cli                 # small meta package and `prose` launcher
@openprose/prose-cli-darwin-arm64
@openprose/prose-cli-darwin-x64
@openprose/prose-cli-linux-x64-gnu
@openprose/prose-cli-linux-arm64-gnu
@openprose/prose-cli-linux-*-musl    # where supported
@openprose/prose-cli-win32-x64
@openprose/prose-cli-win32-arm64     # where supported
```

Platform packages contain Bun standalone executables built with
`bun build --compile`. The exact-version packages are optional dependencies of
the meta package and use npm `os`, `cpu`, and `libc` selectors. A tiny launcher
resolves the installed platform package, forwards signals, and preserves the
binary exit status. It performs no runtime download and requires no
`postinstall` script, so `--ignore-scripts` installations remain valid.

Before Phase 1 packaging, the repository pins the exact Bun version and compile
flags, a Node `engines` floor for the meta launcher, and the minimum supported
npm version after `libc`-selector install tests. The launcher resolves only the
exact platform package version. If optional dependencies were omitted or the
platform is unsupported, it reports the expected package name and a corrective
reinstall command; it never downloads or compiles at runtime. Rust and npm
installation tests use separate prefixes and an explicit executable under test
so two products named `prose` are never accidentally co-installed.

The same Bun executables are published as GitHub Release assets. Package naming
and historical compatibility aliases are confirmed before public publication.

### 14.2 Historical recovery

The early-May npm CLI is reference material, not an implementation authority.
The useful baseline is the `tools/cli/` tree around commit
`5e1d5b49e22379765c2c053c8d94b4f1c6c946af` in
`https://github.com/openprose/prose`, before the Codex subprocess harness was
removed.

Candidates to recover after review:

- CLI/help conventions;
- SDK event-stream handling;
- process-runner tests;
- package/install/release tests;
- harness smoke-test structure; and
- actionable diagnostics.

The following are deliberately not resurrected:

- automatic skill installation/loading;
- command-specific OpenProse semantics in CLI code;
- runner-owned compile/serve/status behavior;
- ambient user/project configuration loading;
- weak single-process cancellation;
- Node-only runtime constraints; and
- any default that silently changes billing.

Recovered code first receives a characterizing test against the historical
behavior, then is adapted to the new shared contract. Copying the old tree
wholesale is not acceptable.

## 15. Test-driven development

### 15.1 Test-first rule

Every runtime-observable behavior begins as one shared black-box conformance
case. Schema examples establish data shape only. Rust and Bun then implement
the behavior independently. Implementation unit tests may be language-specific,
but they do not replace shared conformance.

Test authority has one location per concern:

- `cli/shared/schemas/` defines normative runner invocation, adapter,
  transport-event, result, and error shapes only;
- runner cases under `cli/conformance/cases/` define normative outer-runner
  observable behavior only;
- `cli/shared/fixtures/` contains immutable input assets only; and
- generated expectations are derived from those authorities and are never
  separately hand-maintained.

Each behavior wave lands as two lead-owned integration commits. The first adds
the oracle/schema and records the red commit/tree, command, exit, and output
digest in `cli/protocol/tasks/`; the second adds the implementation that makes
both products green. The red commit remains unsquashed or its replayable patch
and tree digest are retained as a repository artifact. A temporary
implementation scaffold may exist before an oracle only when it has no
externally observable behavior.

A behavior change is incomplete until:

1. its shared fixture fails against the relevant implementation;
2. the implementation makes it pass;
3. the other implementation passes the same contract or honestly reports the
   shared adapter identity as not implemented; that exception applies only to
   an adapter absent from that product, never common CLI, configuration,
   output, or lifecycle behavior;
4. differential conformance detects no undeclared drift; and
5. user/agent-facing help and error fixtures are updated.

### 15.2 Test layers

1. **Pure unit tests** — option parsing, config precedence, invocation
   preservation, hashing, descriptor validation, event normalization, and
   error translation.
2. **Independent fake-harness tests** — a separately owned fixture executable
   simulates structured output, failures, cancellation, malformed protocols,
   and descendant processes.
3. **Shared black-box conformance** — both built CLIs run the same cases in
   isolated environments.
4. **Differential conformance** — normalized results, stdout/stderr, exit
   classification, and effects are compared across the Rust and Bun products.
5. **Package/install tests** — tests invoke a packed npm installation and an
   unpacked Rust release archive, never source-tree shortcuts.
6. **Opt-in real-harness tests** — pinned, cost-bounded smoke and semantic
   conformance against installed/provider runtimes.
7. **Release replay** — the exact promoted artifacts rerun the required local
   and real-harness matrices.
8. **Architecture boundary test** — dependency allowlists forbid imports from
   language/kernel packages, scan adapter interfaces for anything beyond opaque
   image bytes plus the closed task envelope, and fail on source/program
   parsing dependencies.

### 15.3 Hermetic test environment

Every ordinary test receives:

- a fresh workspace, home, config root, cache root, temporary root, and fake
  `PATH`;
- an allowlisted environment with no real provider credentials;
- fixed clocks and ID sources where observable;
- no network;
- bounded time, line length, event size, output, and memory;
- platform-normalized paths;
- a post-test descendant-process audit; and
- deterministic image and invocation bytes.

Tests inject fake transports and poisoned proxy/resolver configuration by
default. At least one authoritative Linux CI lane also enforces network denial
with an OS network namespace/firewall. Other platforms verify that the suite
has no network dependency without overstating hard OS-level isolation.

Required adversarial fixtures include:

- all configuration-precedence combinations;
- missing, unauthenticated, and incompatible harnesses;
- Unicode, whitespace, leading-dash, newline, and shell-metacharacter argv;
- exact source-image bytes and decoded adapter-input content at the declared
  observable boundary;
- duplicate ambient skill suppression;
- stdout/stderr separation;
- fragmented and CRLF JSONL;
- malformed, oversized, duplicated, reordered, and truncated events;
- EOF without a terminal event;
- nonzero exit after a nominal terminal result;
- cancellation before spawn, during startup, during a tool call, and after a
  terminal race;
- a child that ignores cancellation;
- hidden descendants and cleanup failure;
- wrapper recursion markers, harness symlinks, and child invocation;
- prompt-channel downgrade refusal;
- environment and diagnostic secret redaction; and
- no silent harness or billing fallback.

### 15.4 Image tests

CI extracts the Rust archive and every Bun/npm platform artifact and verifies:

- the same image format and semantic versions;
- the same ordered payload manifest;
- the same aggregate digest;
- the same terminal schema; and
- successful sentinel-image replacement without adapter code changes.

### 15.5 Real-harness tests

Real suites are opt-in locally, scheduled/nightly, and required for release
promotion. Each records the exact harness version, adapter transport, model,
auth category, image digest, program digest, platform, and limits.

They run only allowlisted, disposable programs with no consequential external
effects. They assert observable outputs and workspace effects rather than
transcript equality. A quarantine requires an owner, reason, and expiration.

A versioned release-profile manifest names required
harness/transport/platform/version combinations, semantic corpus and image
versions, trial count, retry policy, cost ceiling, and quarantine policy. A
release cannot redefine this matrix after seeing results.

## 16. Prose Complete conformance

Transport conformance and semantic VM conformance are separate.

### 16.1 Transport conformance

The transport suite emits two distinct claims:

- `base-transport-compatible` proves argument preservation, documented prompt
  request construction, structured framing, and terminal process handling for
  research; and
- `strict-wrapper-conformant` additionally proves manifest-permitted image/task
  placement, required ambient suppression, managed safety/auth preservation,
  explicit billing identity, bounded streaming, strict containment/cancellation,
  and reliable carriage/recovery of the language terminal envelope.

Only the strict claim is eligible for a stable semantic adapter listing. The
deliberately nonsemantic functional-alpha profile is narrower: ordinary
release-profile products may run the four frozen adapters only with `echo-v0`,
while retaining strict-wrapper, semantic, benchmark, and public-release claims
as false. The four mandatory installed-process adapters must still earn strict
conformance in both products before a semantic public release.

Every claim names its image and terminal-schema digests. A sentinel-bound run
may prove strict transport mechanics during development, but it cannot support
a stable listing, semantic claim, or release; release admission reruns the same
suite with the canonical language-owned inputs.

### 16.2 Semantic VM conformance

Runs the same Skill Runtime Image and portable OpenProse programs through each
VM candidate. The corpus is language-owned or language-approved and includes,
as it becomes available:

- ordinary result production;
- nested subagent call/return;
- parallel fan-out and complete join;
- isolation canaries;
- higher-order composition;
- budget and cancellation behavior;
- reference-scoped inputs;
- failure propagation;
- deterministic artifacts and terminal evidence; and
- version/reporting behavior.

Mechanical validators are authoritative where possible. Model judges may
assess irreducibly semantic qualities, but their identity, prompt, rubric, and
variance remain visible.

Semantic validators are language-owned, versioned artifacts supplied with the
semantic corpus. CLI conformance and benchmark code may execute them and record
their outputs, but must not parse OpenProse source, derive Return obligations,
or implement reusable OpenProse validity rules. Fixture-specific oracles may
compare declared files, bytes, schemas, and exit envelopes named explicitly by
that fixture; these expectations are not a second language implementation.

The image's task-envelope schema, semantic terminal schema, corpus, validators,
and expected semantic outcomes retain their normative home in the
language-owned image/corpus release. `cli/` may vendor digest-pinned copies as
immutable inputs and execute them generically, but does not code-generate
product semantic types from them or edit their expectations. Q2 owns execution,
collection, and reporting glue only, not semantic expectations or validators.

The sentinel image is sufficient only for transport claims. No semantic-corpus
or Prose Complete claim is made until the canonical Skill Runtime Image and
terminal-envelope schema are available as phase entrance artifacts.

The deterministic `mock` adapter can pass base transport compatibility only. It is
never reported as a Prose Complete semantic VM.

### 16.3 Claims

Reports distinguish:

- `base-transport-compatible`;
- `strict-wrapper-conformant`;
- `semantic-corpus-pass` with corpus/image versions;
- `prose-complete` for a named profile; and
- explicit unsupported or advisory capabilities.

No evergreen, versionless compatibility claim is permitted.

## 17. Benchmark rig

The benchmark system runs installed artifacts black-box and never collapses
all results into one leaderboard score. Phase 6 candidate runs are local and
non-authoritative; only immutable Phase 7 release-candidate runs may be
published.

Official benchmarks live only under `cli/benchmarks/` and invoke ordinary
installed `prose` commands. The v1 runners expose no benchmark command, so an
implementation cannot special-case an official benchmark invocation.

### 17.1 Scorecards

#### CLI and installation experience

- clean installation success;
- time to first valid command;
- package/archive size;
- cold and warm local-command startup;
- configuration/login friction;
- diagnostic task success;
- update and uninstall behavior;
- offline/mock usability; and
- cross-platform success.

#### Transport overhead

- process detection and spawn/connect time;
- image load, integrity-verification, and delivery time;
- time to first normalized event;
- JSONL/RPC/ACP/network framing overhead;
- peak RSS and CPU;
- cancellation and process-tree cleanup latency; and
- residual wrapper time with a scripted agent.

#### Agent efficiency

- total wall time;
- provider and tool spans where observable;
- model calls, retries, tokens, and cost;
- context/image bytes;
- time to first normalized assistant content and, when available, first
  validator-accepted output; and
- wrapper residual separated from model/tool time.

#### Semantic quality and portability

- program completion and required-output validity;
- workspace outcome;
- subagent topology/isolation behavior;
- failure and cancellation correctness;
- evidence completeness;
- success variance over repeated trials; and
- cross-harness observational equivalence.

### 17.2 Trust controls

- Only immutable, conformance-passing release-candidate artifacts are eligible
  for official benchmarks; publication promotes those bytes into release
  artifacts without rebuilding.
- Trials are paired, interleaved, and randomly ordered.
- Cold and warm caches are separate and identically prepared.
- Harness, model, auth category, billing owner, program, image, and artifact
  versions are pinned and recorded.
- Failed and timed-out trials remain in the dataset.
- Every timing sample remains in the dataset; the versioned language-owned
  oracle labels semantic validity rather than accepting or discarding trials.
- True holdouts live outside the implementation worktree, are injected only by
  the benchmark steward through a protected release job, and are published and
  rotated after evaluation. Repository cases under `conformance/adversarial/`
  are visible stress tests, not hidden holdouts.
- Implementations must not detect benchmark fixture names or environment flags.
- The benchmark runner and holdouts have independent ownership from CLI
  builders.
- External wall-clock/resource measurements corroborate internal timestamps.
- Raw JSON, trial order, failures, machine/OS details, digests, and summary
  calculations are published.
- Reports use distributions and confidence intervals, never a best single run.
- Missing subscription token/cost information is `unavailable`, never guessed.
- Model judging is blinded to implementation identity and secondary to
  deterministic validators where possible.

A steward-owned, versioned benchmark policy freezes repetitions, randomized
pairing seed handling, warmups, timeout accounting, a no-outlier-drop rule,
confidence calculation, machine qualification, and per-metric regression
budgets before release-candidate results are visible.

Residual wrapper time is published only when all subtracted provider/tool
spans are complete and share a monotonic clock or an independently validated
clock correlation. Otherwise the report publishes component spans without
subtraction. Every accepted-output metric records the validator and corpus
version that defined acceptance.

## 18. Developer and agent experience requirements

Both products expose identical:

- help examples and command spelling;
- configuration keys and precedence;
- baseline harness/transport IDs and naming rules; research-only IDs are
  explicitly namespaced and reported as implementation-specific;
- error codes and corrective actions;
- quiet, verbose, JSON, and JSONL modes;
- dry-run and diagnostic fields;
- recursion behavior;
- version/image reporting; and
- migration/deprecation policy.

`prose cli doctor --json` is a first-class agent surface. It reports readiness,
detected versions, selected prompt channel, isolation strength, auth category,
billing owner, image digest, and actionable problems without exposing secrets.

A no-execution dry run shows the selected harness/transport, executable or
embedded runtime version, configuration sources, image digest, prompt
placement, working directory, permission posture, and billing owner.

Non-TTY output has no spinner, cursor control, or color by default. Errors lead
with the failed boundary and the command that fixes it. Installation and login
instructions are copyable. Machine output never requires scraping prose.

## 19. Multi-agent implementation protocol

### 19.1 One worktree, disjoint ownership

Implementation occurs in this single worktree. Collision avoidance comes from
exclusive path ownership and integration windows, not concurrent Git branches.

- The lead owns `cli/SPEC.md`, `cli/protocol/STATUS.md`,
  `cli/protocol/OWNERSHIP.md`, `cli/protocol/decisions/**`, `cli/AGENTS.md`,
  root-level changes, and final integration.
- The contract/image integrator owns the specifically leased schema, image,
  capability, error, and generator paths under `cli/shared/**`; it does not
  write generated bindings into product trees.
- Rust agents edit only their assigned crate or adapter directories under
  `cli/rust/**`.
- Bun agents edit only their assigned directories under `cli/bun/**`.
- Transport-oracle and semantic-oracle agents receive distinct leaf paths
  under `cli/conformance/**`; neither owns the whole tree.
- Benchmark agents exclusively own `cli/benchmarks/**` and do not edit product
  code.
- Delivery agents own `cli/ci/**`, `cli/release/**`, and, only in the final
  phase, uniquely named root workflow stubs.

No subagent may run `git add -A`, commit, stash, checkout, reset, or perform a
root-level package install. The lead owns the Git index and commits. Formatting
and generation are path-scoped.

Shared manifest or dependency changes happen in scheduled integration windows.
The registry assigns each exact manifest or lockfile to one time-bounded lease;
parallel agents request such a change rather than editing it. Dependencies
needed for a wave are predeclared before fan-out where possible.

The lead maintains an active ownership/lease registry in
`cli/protocol/OWNERSHIP.md` with task ID, exact writable paths, any time-bounded
shared-file lease, and terminal state. An agent stops if a formatter, code
generator, or package tool proposes a write outside its lease. Generated
bindings are either written to untracked build directories or committed by the
owning Rust/Bun implementation agent; C1 owns the schema and generator only.

### 19.2 Roles

- **Lead architect/integrator:** freezes contracts, decomposes waves,
  adjudicates cross-cutting changes, runs admission tests, and commits.
- **Contract/image integrator:** schemas, binding generator, image verifier;
  no cross-product binding writes and no language prose authoring.
- **Builder:** one core or adapter surface and its unit tests.
- **Oracle author:** shared black-box cases; no product implementation.
- **Verifier:** reviews and runs tests for work it did not author.
- **Adversary:** process, prompt-precedence, auth, portability, and benchmark
  attacks.
- **Benchmark steward:** measurement runner, holdouts, statistics, raw reports.
- **DX steward:** installation, diagnostics, help, and agent-facing behavior.
- **Release steward:** packaging, provenance, CI, and promotion after local
  readiness.

Independent verification is never lighter than implementation. A builder does
not author the only oracle that admits its own behavior.

### 19.3 Task contract

Every delegated task names:

- exact owned paths;
- frozen shared schema/image versions;
- allowed dependencies;
- tests/oracles it must pass;
- prohibited paths;
- expected output; and
- one terminal state: `green(evidence)`, `blocked(reason)`, or
  `failed(reason)`.

The repository is sufficient to resume after context compaction. Status,
decisions, and task terminals live under `cli/protocol/`; no load-bearing
decision remains only in chat.

### 19.4 Planned workstreams

| Workstream | Exclusive surface |
| --- | --- |
| C1 shared protocol/image generator | leased `cli/shared/schemas/**`, `image/**`, `capabilities/**`, `errors/**` |
| Q1 fake harness and transport corpus | `cli/conformance/fake-harness/**`, `fake-hosted-service/**`, `cases/transport/**`, `adversarial/transport/**`, `cli/shared/fixtures/transport/**` |
| R1 Rust core/UX/config | `cli/rust/crates/prose-runner-core/**`, `prose-cli/**` |
| B1 Bun core/UX/config | `cli/bun/src/core/**`, CLI entrypoint |
| R2 Rust process supervisor | `cli/rust/crates/prose-process-supervisor/**` |
| B2 Bun process supervisor | `cli/bun/src/supervision/**` |
| R-Prime/B-Prime | separate Rust/Bun Prime adapter paths, paired by Q1 oracle |
| R-OMP/B-OMP | separate Rust/Bun OMP adapter paths, paired by Q1 oracle |
| R-Codex/B-Codex | separate Rust/Bun Codex adapter paths, paired by Q1 oracle |
| R-Claude/B-Claude | separate Rust/Bun Claude adapter paths, paired by Q1 oracle |
| E1 Bun embedded/SDK adapters | separate adapter directories per SDK |
| E2 Rust ACP transport | dedicated Rust ACP crate |
| E3 Rust Rig experiment | dedicated experimental Rust crate |
| H1 OpenProse default/auth/protocol | dedicated hosted adapter code only; Q1 owns `cli/conformance/fake-hosted-service/**` |
| Q2 semantic conformance coordinator | `cli/conformance/cases/semantic/**`, `cli/shared/fixtures/semantic/**` |
| M1 benchmark system | `cli/benchmarks/**` |
| D1 install/diagnostic/documentation | assigned non-overlapping DX paths |
| P1 CI/build/release | `cli/ci/**`, `cli/release/**`, final workflow stubs |

The lead uses successive waves sized to the available concurrency. Parallel
work occurs only where owned paths and frozen interfaces make it safe.
Slash-paired adapter labels denote two independent implementation tasks with a
common oracle, never one agent editing both product trees.

## 20. Implementation sequence and gates

### Phase 0 — specification and contract freeze

- Ratify this runner boundary.
- Create scoped `AGENTS.md` ownership instructions.
- Define shared runner schemas, error taxonomy, fixtures, and binding
  generators.
- Define the image packaging interface using a sentinel image.
- Record the current canonical-image conflict as an external release gate.
- Freeze the provisional Tier-1 matrix: macOS arm64/x64, Linux x64/arm64
  glibc, and Windows x64. Linux musl and Windows arm64 begin optional; removal
  of a Tier-1 target requires a decision record and acceptance-criteria update.
- Freeze toolchain/package pins, adapter admission-recipe format, release
  profile, and benchmark-policy schemas before builder fan-out.

**Exit:** both implementation skeletons can consume generated shared types and
the fake harness can observe the exact task and image inputs.

### Phase 1 — local walking skeletons

- Build Rust and Bun CLIs with identical help/version/config behavior.
- Add `mock`, `prose cli doctor`, `prose cli harness list`, dry-run, and JSON
  output.
- Run both against the same fake harness.
- Pack/install both locally and invoke installed artifacts.

**Exit:** one local command runs unit, black-box, package, and differential
tests with no network or credentials.

### Phase 2 — process supervisors

- Implement direct process execution, version probes, structured streams,
  cancellation, timeouts, process-tree cleanup, environment policy, prompt
  files, and recursion protection.
- Complete the adversarial process corpus on the applicable provisional Tier-1
  platforms. Mandatory installed-process adapters require strict containment
  on every Tier-1 platform; a platform must be formally demoted through the
  Phase 0 decision-record rule before this phase can exit.

**Exit:** no contained orphan processes after catchable termination, no
shell/PTY path, verified canonical input delivery at the declared boundary,
and identical error behavior.

### Phase 3 — four BYO installed harnesses

- Prime Agent;
- OMP;
- Codex;
- Claude Code.

Each starts with the simplest documented structured one-shot interface. Richer
RPC/app-server/ACP variants follow where they improve measured capability.

**Exit:** all four earn strict-wrapper conformance. If and only if the canonical
Skill Runtime Image and terminal-envelope schema have entered the phase, at
least two distinct harnesses also run the initial semantic/subagent corpus;
otherwise semantic admission remains gated without blocking transport work.

### Phase 4 — SDK and embedded bake-off variants

- Mandatory research attempts: Bun Codex SDK, Bun Claude Agent SDK,
  Pi/OpenRouter, Bun OMP embedded, Rust ACP transports, and Rust Rig.
- Optional research attempts: OpenAI Agents SDK reference and direct Prime
  package integration.

**Exit:** every mandatory attempt has either a working adapter or a reproducible
decision report naming its blocker. Every working/claimed variant has explicit
auth/billing identity, packaging result, base/strict transport status, semantic
profile, and benchmark eligibility. Failed research does not block the shared
four-process baseline and is never hidden.

### Phase 5 — OpenProse default

- Complete the execution-placement decision record.
- Implement account login/token storage and billing/quota evidence.
- Implement the chosen local/hosted capability boundary.
- Prove Rust/Bun parity against the scripted service before model-backed use.
- Run the semantic corpus with the same release image.

**Exit:** `prose run ...` works after OpenProse login with no third-party key
and no silent fallback, the adapter is strict-wrapper-conformant, and it passes
the named minimum semantic release profile.

### Phase 6 — trusted benchmark and DX hardening

- Run paired transport, agent, semantic, installation, and DX scorecards on
  local candidate artifacts only.
- With the canonical image and terminal schema, run the named minimum semantic
  release profile for the OpenProse default and all four stable BYO baseline
  adapters; a failing adapter must leave the stable release surface rather than
  silently lowering the profile.
- Prepare protected external holdouts and adversarial benchmark review.
- Fix the highest-impact portability and error-actionability gaps.
- Produce a public-format raw report without publishing it yet.

**Exit:** both candidates are locally buildable/installable, conformance
reports are honest, the bake-off is reproducible, and no public performance
claim has been made.

### Phase 7 — CI, build, and release automation (last)

Implementation note: the path-scoped CI and protected manual draft-only graph
now exist. The graph deliberately stops at preflight for the release-ineligible
sentinel; native execution evidence, canonical language inputs, independent
supply-chain authorities, publication, and promotion remain outside current
admission.

Only after the local gates are green:

- add uniquely named root GitHub workflow stubs;
- run PR test matrices for Rust, Bun, shared conformance, install smoke, and
  differential behavior;
- build immutable release-candidate platform artifacts once;
- verify, run required real-harness conformance, and run trusted benchmarks on
  those exact digests;
- generate checksums, SBOMs, and provenance;
- create a draft GitHub Release and upload the verified assets without
  promotion;
- publish and verify npm platform packages, then publish and verify the exact
  meta package;
- promote the GitHub Release last; and
- retain release manifests and benchmark/conformance reports.

No public push, release, package publication, or repository-governance change
occurs without explicit user authorization.

## 21. CI and release requirements

Before Phase 7, the following gates are documented local admission commands
under `cli/ci/`; no root workflow file may exist until the Phase 6 local exit
is recorded. Phase 7 turns the already-green commands into GitHub automation.

### 21.1 Pull-request gates

- shared schema/image validation;
- architecture-boundary dependency/interface validation;
- generated-binding drift check;
- Rust format, lint, unit, integration, and black-box tests;
- Bun format, lint, typecheck, unit, integration, and black-box tests;
- differential Rust/Bun conformance;
- cancellation/orphan safety tests;
- local package/archive installation smoke;
- dependency, vulnerability, and license policy checks; and
- deterministic benchmark smoke budgets.

Linux, macOS, and Windows run the applicable conformance matrix. A shared
change runs both implementations.

### 21.2 Release artifacts

One product version releases both implementations and records the independent
Skill Runtime Image version.

Artifacts include:

- Rust archives for supported OS/architecture targets;
- Bun standalone binaries for the same supported targets;
- npm meta and platform package tarballs;
- SHA-256 manifest;
- SBOMs;
- provenance/attestations;
- signed checksum material where practical;
- conformance report;
- benchmark metadata/raw-results bundle; and
- release notes with known capability differences.

Build once and promote the tested bytes. Do not rebuild after verification.
Toolchains, dependencies, and Actions are pinned. Publication uses least
privilege and protected environments; npm uses trusted/OIDC publishing when
available.

The release manifest binds the source commit, toolchain versions, Skill Runtime
Image digest, every archive/package SHA-256, SBOM digest, conformance-report
digest, benchmark-report digest, and attestation subject digest.

npm publication stages immutable platform tarballs first, verifies their
registry digests, publishes the exact-version meta package last, verifies its
digest, and only then promotes the draft GitHub Release. A partial npm version
is deprecated and documented and is never silently reused. Initial public
publication requires explicit manual dispatch/approval through a protected
environment; creating a tag alone cannot publish.

## 22. Acceptance criteria

The project is complete when all of the following are true:

1. **External compatibility invariant:** the language team's direct-skill
   regression suite shows that direct TUI invocation works with no `prose`
   binary on `PATH`; CLI artifacts neither implement nor become a prerequisite
   of this behavior.
2. Wrapper invocation works with no OpenProse skill installed in the selected
   harness.
3. Neither entry path invokes the other.
4. Rust and Bun embed the same Skill Runtime Image bytes, manifest, version,
   and digest and report the same declared normalization boundary on delivery.
5. Replacing the image with a sentinel requires no launch-adapter code change.
6. Runner code has no Contract Markdown, ProseScript, Forme, frame, Return,
   state-backend, or semantic-evidence dependency.
7. The runner may read closed runner configuration and explicitly supplied
   transport files, but does not inspect Contract Markdown, ProseScript,
   generated language state, or program contents to decide how to launch.
8. Current and unknown future language commands, including `cli` when forced
   through `prose -- cli ...`, share one opaque forwarding path.
9. Argument boundaries survive Unicode, whitespace, leading dashes, and shell
   metacharacters without shell execution.
10. No wrapper transport launches a harness TUI.
11. Every stable semantic adapter suppresses the explicitly required ambient
    harness skills/instructions without disabling managed safety policy,
    user-selected permission posture, or intended authentication. The narrower
    functional-alpha exception may run only the four frozen adapters with
    `echo-v0` and must report strict and semantic claims as false; other weaker
    research adapters are not runnable by the ordinary product.
12. External Prime, OMP, Codex, and Claude adapters pass shared transport
    tests and earn strict-wrapper conformance in both products.
13. Every mandatory Bun SDK/embedded and Rust ACP/Rig research attempt ends in
    a working versioned report or a reproducible blocker report; differences
    never excuse drift in shared adapter identities.
14. `openprose` is the default, uses OpenProse billing, and never silently
    falls back.
15. Missing executables, auth, incompatible versions, bad images, malformed
    protocols, unknown semantic status, cancellation, and cleanup failure have
    distinct actionable errors.
16. Catchable cancellation leaves no process retained by the runner's declared
    containment mechanism; detachment and cleanup failure are detected and
    reported without risking unrelated processes.
17. The mock is never described or selected as a semantic fallback.
18. Both locally installed deliverables pass the same black-box suite.
19. Differential conformance has no undeclared behavior drift.
20. Cross-harness semantic testing uses the same language image and programs
    and evaluates observable outcomes rather than transcript equality.
21. Benchmark results retain failures, publish raw metadata, and keep DX,
    overhead, cost, and semantic quality separate.
22. A clean npm global installation runs the Bun deliverable without a runtime
    download or postinstall requirement.
23. GitHub Release artifacts and npm packages are the exact conformance- and
    benchmark-tested bytes and carry bound checksums, SBOMs, provenance,
    version metadata, and the image digest.
24. The full local verification suite runs with one documented command before
    CI or provider access is needed.
25. No public artifact contains the sentinel image. The canonical Skill
    Runtime Image and language-owned terminal schema are digest-pinned release
    inputs, and the OpenProse default plus every stable BYO baseline adapter
    meets the named semantic release-profile threshold before promotion.

## 23. Deferred decisions and external gates

These do not permit semantic assumptions in implementation:

- canonical Skill Runtime Image contents;
- skill-owned structured terminal envelope details;
- strict/canonical-image admission of the frozen Prime/OMP prompt-injection
  controls and version ranges (their functional-alpha RPC recipes are fixed);
- final OpenProse default execution placement;
- hosted identity, endpoints, pricing, quotas, retention, regions, and spend
  controls;
- public npm package scope/compatibility alias;
- promotion of Windows ARM64 and Linux musl beyond optional support;
- redistribution constraints for SDK-bundled third-party executables;
- which rich transport becomes canonical for each harness; and
- whether optional adapters remain statically bundled or ship as supervised
  adapter drivers.

Firm decisions are: two independent entry paths; no TUI; no recursion; one
opaque skill-owned language image; launch adapters do not implement the
language; explicit authentication and billing identity; no silent fallback;
shared test and benchmark authorities; Rust/Bun shared-surface and baseline
parity with explicitly implementation-specific research variants; one isolated
repository subtree; local functionality before public automation; and
evidence-backed portability claims only.

## IMP-034: explicit staging account commands

The opt-in global `--service-environment staging` admits only `cli auth login`,
`cli auth status`, `cli auth logout`, and `cli org list`. Other values or uses
are invocation errors. Without this option existing unavailable account behavior
and all harness routing remain unchanged. This does not enable hosted execution.
The only network origin is `https://run-prose-staging.openprose.workers.dev`;
redirects and user-configurable token destinations are forbidden.

The shared black-box corpus is `conformance/runner/staging-service-corpus.json`.
Staging account and organization results use the closed `service-account` and
`organization-list` schemas. Service errors are contained in `problem`, with
exit code 10; cancellation uses existing `CANCELLED` and exit 24. No raw remote
errors, device codes, API keys, or unknown response fields enter output.
Organization projection contains only id, slug, name, and optional role.

`OPENPROSE_STAGING_API_KEY` overrides only the staging local credential. It is
never forwarded to a harness. Login/logout reject this variable when nonempty,
because they cannot replace or clear the parent environment. Local credentials
use OS credential storage, service `org.openprose.cli.staging`, account
`api-key`; unavailable native storage fails closed with no plaintext fallback.
Status without a token succeeds as signed out. Status with a token verifies it
using GET `/organizations`; this endpoint can lazily create the account's default
organization. Logout removes only the local credential; it does not revoke a
server key. No provider credential is consulted or modified.

Device login POSTs `/auth/device` without authorization, prints the returned
user code and exact `https://github.com/login/device` verification URI on stderr,
and POSTs `{device_code}` to `/auth/device/poll`. It does not open a browser.
The start response requires expiry 1..900 seconds and polling interval 1..30
seconds. Pending waits the current interval; slow_down adds five seconds capped
at 30. The operation stops at expiry or 180 polls, on cancellation, on remote
error, or on complete. Complete requires a nonempty API key, stored only in the
native credential store. Expired remote tokens and local deadlines normalize to
DEVICE_AUTH_EXPIRED; other device errors to DEVICE_AUTH_FAILED. HTTP 401/403
normalize to SERVICE_AUTH_REQUIRED except device error responses; HTTP transport
or 5xx failures to SERVICE_UNAVAILABLE; malformed successful responses to
SERVICE_PROTOCOL_INVALID. Requests have bounded time and response sizes and
never retry by changing origins or credentials.

Only compiled test-seam builds may honor `PROSE_TEST_SERVICE_FIXTURE`. The file
contains `credential`, `storeAvailable`, ordered `exchanges` with exact `method`,
`path`, HTTP `status`, and JSON `body`, plus optional `cancelBeforePoll`. This
transport consumes the transcript without network, uses a virtual monotonic
clock, and replaces the credential store in memory. It is not an endpoint
override. Requests must match transcript methods and paths and use a bearer
credential only for organization requests. Release builds ignore this seam.

The staging credential predicate is exactly `rr_test_[0-9a-f]{32}`; invalid
credentials normalize to SERVICE_PROTOCOL_INVALID. Device user codes are
1..32 characters from `[A-Z0-9-]`, and must not contain the private device code.
Projected organization strings are nonempty, at most 4096 Unicode scalar values,
contain no ASCII control characters or DEL, and cannot contain the bearer key.
Extra organization fields, including nested private fields, are discarded.
Malformed transcript roots fail SERVICE_PROTOCOL_INVALID: credential must be
null or a string, storeAvailable a boolean, exchanges an array of at most 182.

## IMP-034 persistent service environments (supersedes staging-only admission)

Production-default service environment contract, supersedes staging-only paragraph:
- Grammar: prose cli environment show [--json]; prose cli environment use staging|production [--json]; prose cli environment reset [--json]. Global --output json supported. Reset removes stored selection =>production. Use production stores explicit production. Commands require no auth/network/keychain.
- Persistent flat service_environment="staging"|"production" in existing USER cli.toml only. Use existing safe atomic writer preserving other values/comments and validate same protections. No project setting may redirect service. Service resolver reads user only, ignores project; normal project config with service_environment rejects CONFIG_INVALID. Keep service setting out of harness EffectiveValues/config-explain schema, expose via environment show.
- Unset=>production. All auth login/status/logout and org list work by default, no flag needed. Retain --service-environment production|staging as ephemeral account/org override only; reject with environment management or unrelated ops. No generic endpoint env override.
- Production origin https://run-prose-production.openprose.workers.dev; staging https://run-prose-staging.openprose.workers.dev. All OpenProse account/service requests route through selected fixed origin. Model/provider/kernel artifact endpoints unchanged.
- Credentials: OPENPROSE_API_KEY production; OPENPROSE_STAGING_API_KEY staging. Only selected variable consulted. Both filtered from ALL harness/probe env even explicit reallow. Both namespaces independent org.openprose.cli.production and org.openprose.cli.staging account api-key. Backend actually issues rr_test_32hex on BOTH deployments, do not use rr_live_. No fallback across env. Login/logout reject only selected envtoken. Switching never reads/writes/deletes creds.
- Human account/service and environment command output for staging clearly includes 'OpenProse staging'. JSON stays single parseable object with environment production|staging (no banner); device verification stderr remains necessary but contains no credential. Other local/harness human outputs need not change because they make no OpenProse service request.
- Environment command closed output: schema openprose.service-environment/1, environment production|staging, source default|user-config, problem null|runner-error. Success show/use/reset returns selected env+source. Failure invalid config normal existing runner-error envelope allowed; never default silently if malformed user config. service-account/organization-list schemas environment enum both.
- Test seam may optionally contain environment:'production'|'staging', credentials:{production: token|null,staging:token|null}; old credential applies only expected environment when provided. Assert selected env equals fixture.environment if supplied and exchange optional origin equals selected origin. Never reach network when fixture present or credential real stores.
- Shared sequences will test persisted use/show/status/reset, fresh process, precedence, wrong env tokens, fixed origins, malformed/project config, JSON/human indicator. Update existing no-flag account tests to hermetic fixture behavior BEFORE full tests to avoid actual network/keychain.
