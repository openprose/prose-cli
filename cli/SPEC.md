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
shell runner. `prose help cli [COMMAND...]` is the one exception: `cli` is
reserved, so it is never a language help topic, and the runner prints the
matching `cli` help topic.

The bare first token `cli` is the one permanent operational reservation.
`prose -- cli ...` strips the delimiter and forces `cli ...` through the
language path, preserving a deliberate escape hatch.

One narrow exception protects agents that omit `cli` ("Missing `cli`" below). The first words after the
runner globals may spell a service or runner command path (`run submit`,
`job list`, `wallet balance`, `org member list`, `doctor`). If neither
of the first two words names an existing file or directory relative to the
working directory, the runner exits 2 with the `prose cli ...` command instead
of forwarding. The same applies to a lone word that a `nounSynonyms` entry
maps to exactly one command (`login`, `whoami`, `models`), to a verb before
its group (`list jobs`) and to a `commandRewrites` word (`stop`, `delete`,
`share`, `cron`). A language command that also names a service command
(`status`, `help`, `examples`) is forwarded unless the default hosted harness,
which runs no language command, would refuse it; `run FILE` is always
forwarded, and `prose -- <WORDS>` always forwards.

A second exception protects agents that put a command option first. When the first token the runner-global parser does not
consume is a command-local option (a manifest `commonOptions` flag, an
operation option, or an `optionAliases` spelling of one such as `-j` or `-y`),
and only such options and runner globals precede `cli`, the runner exits 2
(`INVOCATION_INVALID`) with `details.suggestedArgv` moving the options after
the command path. `prose --json cli run list` is therefore never forwarded.
An unknown option, or options not followed by `cli`, still freeze runner
parsing as the first opaque language token. `prose help cli ... --json` prints
the same topic as without `--json`.

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

### 7.2.1 Proposed local weave host bridge

This isolated branch proposes `prose cli weave --host-binding ABS OP CONFIG ...`.
It is not a released command. The exact admission, operation grammar, byte-stream,
exit and interruption contract is [the weave host decision](protocol/decisions/weave-host.md),
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

Both products apply one value grammar. Environment booleans are exactly
`true`, `false`, `1` or `0`; durations match `^[1-9][0-9]*(ms|s|m|h)$` and are
at most 24 hours (`maxTimeoutMs` in
`shared/capabilities/transport-limits.v1.json`); an empty value is rejected,
`PROSE_NATIVE_LOG` included; and a value holding U+FFFD (the replacement for
bytes that are not UTF-8) is rejected. Values are checked in one order, the
order of `values` in `shared/schemas/configuration-explanation.schema.json`
(with `nativeLog` before `authProfile`), within each source: a file's values
after all of its lines parse, then the environment, then flags. Every
`CONFIG_INVALID` names its source in `details.source` (`path:line`, the
variable, the flag, or `process cwd`), and reasons are fixed text without
operating-system error strings. On Windows, variable names match without
regard to case. The shared vectors are
`shared/fixtures/config/values-v1.json`.

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

Both products cancel on `SIGINT`, `SIGTERM` and (POSIX) `SIGHUP`, the signal
of a closed terminal: the run is cancelled cooperatively and ends with
`CANCELLED` (`24`). Neither product handles Windows Ctrl+Break, whose default
action ends the process.

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

## Account commands

`cli auth login`, `cli auth status`, `cli auth logout` and `cli org list`
connect the CLI to an OpenProse account. They do not enable harness execution.
Public builds reach exactly one origin, the production service origin
(`environments.production.origin` in `shared/service/operations.v1.json`); redirects and
user-configurable token destinations are forbidden, and no environment
variable, option or configuration value changes the origin (see
"Service origin" under hosted service operations for the separate developer build).

The shared black-box cases are `conformance/cases/service/account/` and
`conformance/cases/service/organizations/`. Account and organization commands
print the `openprose.service-operation/1` envelope like every other `cli`
command (`operation` `auth.status`, `auth.login`, `auth.logout` or
`org.list`); their results are the closed `service-account` (`authenticated`,
`credentialSource`) and `organization-list` (`organizations`) schemas. Service
errors are contained in `problem`, with exit code 10; cancellation uses existing
`CANCELLED` and exit 24. No raw remote errors, device codes, API keys, or
unknown response fields enter output. Organization projection contains only
id, slug, name, and optional role.

`OPENPROSE_API_KEY` overrides the stored credential. It is never forwarded to
a harness, even when explicitly re-allowed. Login/logout reject this variable
when nonempty, because they cannot replace or clear the parent environment.
Stored credentials use OS credential storage, service
`org.openprose.cli.production`, account `api-key`; unavailable native storage
fails closed with no plaintext fallback. Status without a token succeeds as
signed out. Status with a token verifies it using GET `/organizations`; this
endpoint can lazily create the account's default organization. Logout removes
only the local credential; it does not revoke a server key. No provider
credential is consulted or modified. A user `cli.toml` that still carries a
`service_environment` key (written by `cli environment use` in earlier
versions) is accepted and the key is ignored.

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
contains `credentials` (`{production: token|null}`), `storeAvailable`, ordered
`exchanges` with exact `method`, `path`, HTTP `status`, and JSON `body`, plus
optional `cancelBeforePoll`. This transport consumes the transcript without
network, uses a virtual monotonic clock, and replaces the credential store in
memory. It is not an endpoint override. Requests must match transcript methods
and paths and use a bearer credential only for organization requests. Release
builds ignore this seam.

The credential predicate is exactly `rr_test_[0-9a-f]{32}`. A malformed or
rejected key is SERVICE_AUTH_REQUIRED naming the variable (see
"Credential teaching and account options"). Device user codes are
1..32 characters from `[A-Z0-9-]`, and must not contain the private device code.
Projected organization strings are nonempty, at most 4096 Unicode scalar values,
contain no ASCII control characters or DEL, and cannot contain the bearer key.
Extra organization fields, including nested private fields, are discarded.
Malformed transcript roots fail SERVICE_PROTOCOL_INVALID: credentials must be
an object, storeAvailable a boolean, exchanges an array of at most 182.

## Hosted service operations

Status: see `protocol/STATUS.md`.
The normative data is `shared/service/operations.v1.json` (the operation
manifest), the schemas it names, and `shared/errors/taxonomy.v1.json`.

**Scope.** `prose cli` gains runner-owned service operations that reach the
OpenProse hosted service: discovery, pricing, hosted runs, run
records, saved programs, published results, jobs, the wallet and
organizations. They are not harness execution. `prose <FILE>` with the
default `openprose` harness still reports `HOSTED_UNAVAILABLE` with unchanged
text; hosted runs are `prose cli run submit`. Phase 5 (§12) is unchanged.

**Grammar.** `prose [--output human|json|jsonl] cli <noun> <verb> [ARGUMENTS]
[OPTIONS]`. Nouns are singular. Every operation, its
arguments, options, requests, confirmation class, transport class and
result schema are declared by the manifest, and both products derive parsing,
help and confirmation from the embedded manifest. Each operation's `command`
is its argv after `prose` (`["cli", "run", "submit"]`), as in the capabilities
document. `prose cli service operations --json` prints the published manifest
as the envelope's `result` (one line) and makes no request; the published
manifest is the embedded manifest's public projection
(`shared/service/operations-public.v1.json`, an allowlist of members): each
operation's command, arguments, options, spec, effect, confirmation, output
schema names, exit codes and examples, plus the grammar, exit dictionary,
environment variables and status classification. Service routes and their
catalog entries, service error strings, the service origin, the key format,
the vendored export and this client's own transport, journal and identity
settings are read internally and never printed; the projection records a size
budget and strings that must never appear in it.
Human mode prints a summary table (operation, effect, confirm, usage) and
`--output jsonl` prints one `openprose.service-record/1` line per operation,
in manifest order, then the `openprose.service-page/1` trailer. A service
`--help` in a JSON mode (`prose --output json cli run --help`) prints the
envelope whose result is `{help, operations}`: the help text and the manifest
records of the commands it describes. The manifest also declares `exitCodes`, the exit dictionary keyed by
error code (`{exit, retryable, meaning}`, each equal to the taxonomy entry),
`envVars` (every variable service operations read), and per-operation
`examples` (runnable command lines). `prose cli service capabilities [--json]`
prints the grammar, variables, exit dictionary and the noun/verb
index with examples on one page, or as one canonical
`openprose.service-capabilities/1` line (`shared/schemas/service-capabilities.schema.json`);
it makes no request. Both views are rendered once by
`ci/render_service_help.py` into `shared/service/help.v1.json` (`capabilities`,
`views`) and embedded by both products. `prose cli service guide [--json]` prints the agent guide `shared/service/guide.v1.md` byte for
byte, or its `## ` sections as the result `{sections: [{id, title, body}]}` of
one `openprose.service-operation/1` envelope; it makes no request and needs no
key. Both products embed the file; the `service-help` gate fails when a
`prose cli ...` command in it does not parse against the manifest, when a
required section is missing, or when the generated cases pinning both views
are stale. `prose cli service triage [--json]` is one
read of the session state: it sends `GET /health` anonymously, then, with a usable key, `GET /wallet/balance`,
`GET /organizations/default`, `GET /runs?limit=5` and `GET /triggers`. The
result (`service/discovery.schema.json#/$defs/serviceTriage`) has one section
per read, each `{problem}` or its projected fields with `problem: null`, a
`credential` section (`variable`, `source`, `state`, `problem`) and
`nextCommands` (`{why, argv, env}`) derived from state. It exits 0 whenever
the report is produced; only cancellation and invocation errors end it early.
Without a usable key the account sections are null and the credential
problem names the variable to set; after an unanswered `/health` nothing else
is sent. Sections carry price fields only. Human output is at most 25 lines. The `service-help` gate fails when the
dictionary lacks a code that a shared corpus case emits or expects under a
different exit, lacks a code an operation or the error classification names,
disagrees with the taxonomy, or lists an unexplained code; and when an example
names another command, an unknown or missing required option or argument, or
a confirm-class command without `--yes` or `--preview`. The
`service-operations-*` gates also run every example against the product and
fail when its grammar parser rejects one. When stdout is a closed pipe
(`| head -1`), both products stop writing stdout and exit with the command's
own code without a diagnostic. The global `--model`,
`--harness` and `--dry-run` options are `INVOCATION_INVALID` in the global
position before a service operation. Operation options follow the command
path, and an operation may define an option of the same name (`cli run submit
--model`). Trailing `--json` equals `--output json`. No new environment
variable is read for output; `PROSE_OUTPUT` keeps its §7.4 meaning. `--help`
prints the text in `shared/service/help.v1.json` for that topic exactly.
Every manifest command path has a topic, including the account `auth`,
`org list` and `package` commands. `cli <noun> --help`, and
`--help` or `-h` after an account verb, print that topic and never the runner
help. Each operation's "Exit codes:" line is rendered from its manifest
`exitCodes` (`{exit, meaning, codes?}`, ascending). The `service-help` gate
fails when that list disagrees with the error taxonomy, lists a
route-override code for an operation that does not send the route, or
disagrees with the exits and codes that the shared corpora expect from the
operation.

**No prompts; confirmation.** No service operation prompts. `--yes` is required
when any non-GET/HEAD request's catalog interaction has agent policy `confirm`
or `never`, is `DELETE`, has effect `money`, `outward` or `destructive`, or is
not reversible; the manifest may only tighten this. Without `--yes` the
operation sends no mutation and exits 2 with `CONFIRMATION_REQUIRED`, whose
`details.plannedRequest` describes the planned request: its method, what it
does in words (`description`, the first sentence of the operation's summary),
the body digest, the effect, the non-secret `summary` of the caller's inputs
and, for `run submit` and `job create`, the service hold quote. The service
route, its query and the price policy reference are never shown. `--preview` prints the same plan for any
mutation as a successful result, exits 0 and sends no mutation. The account
operations (`auth`, `org list`, `package`) keep their grammar; the manifest records their
confirmation waivers.

**Output.** JSON output is one `openprose.service-operation/1` object followed
by LF. It contains `schema`, `operation`, the CLI id, and `interaction`, the
primary service catalog id or null. It names no service environment (a
developer build's human output names its endpoint, below). `result` is the
closed per-operation projection; a paged operation's result carries
`nextBefore`. `problem` is a runner error; when it is non-null, `result` is
null. A `cli` command's `INVOCATION_INVALID` says what was wrong in its
`message`: `Unknown command.`, `Unknown option.` or `That command isn't quite
right.`; the language and the runner commands keep `Runner invocation is
invalid.`. One JSON error shape: in JSON and JSONL modes this
envelope also carries every invocation error of a service command line that
was rejected before an operation ran: a near miss (`cli models`,
`cli run delete`), a command option before `cli`, a missing `cli`, an invalid
or conflicting `--output`, and an account-path refusal
(`cli package frob`).
`operation` is the operation the argv names, meaning the longest run of
command words after `cli` that equals an operation's command, else the literal
`cli`, whose `interaction` is null. JSONL prints the envelope as one line,
except that a streaming operation prints its one `service.failed` line.
Human output is unchanged. A bare `openprose.runner-error/1` is printed only
for language commands and the local runner commands (`cli doctor`,
`cli harness`, `cli config`, `cli cleanup`). A transport failure
(`SERVICE_UNAVAILABLE` with no service response) carries `details.reason`, so
every non-zero exit of a service command line has `problem.details`; the
service corpus runner asserts both over every case. JSONL streams emit `openprose.service-event/1` lines. The service's
`run_complete` frame becomes exactly one terminal `service.completed`,
`service.failed` or `service.detached` line carrying the envelope, and nothing
follows it; `service.detached` is the exit-21 end (detached or the `--wait`
deadline: the run continues), `service.failed` every other failure. End of
stream after an `error` frame is terminal. Unknown event types become
`unrecognized` events carrying only a sanitized name. Heartbeat comments are
ignored. Human mode writes results and text chunks to stdout, and status,
activity and warnings to stderr. A status line carries the status word only
(the service's status message stays in the JSONL event). An agent's structured
final answer (`{status, reason, semantic_diff}`) prints as its words, never as
JSON. Human errors name no HTTP status (`details.serviceStatus` keeps it); an
allowlisted service code prints as `Service code:`. Results keep service field
names verbatim and are built from allowlists: unknown fields are dropped at
every depth. A run's `environment` is its public id (`builtin`, `linux`); the
service's runtime name, runtime contract and environment version are not
output. Signed file URLs and `customer_id` are never output.

**Lists, times and human output.** A list operation names its
item array in the manifest's `output.records`. With `--output jsonl` it prints
one `openprose.service-record/1` line per item and then one
`openprose.service-page/1` trailer carrying `count`, `nextBefore` (null when
there is no next page) and `meta`, the result without its collection; a failed
list operation prints the envelope. In JSON the cursor is
`result.nextBefore`; error envelopes carry none. Every epoch-ms result field `X` has an additive `X_iso`
(RFC 3339 UTC, or null). Human output uses dollars for money, prints
`No <things>.` for an empty list, flattens nested records into `label: value`
lines (`run show`, the job renderer), prints `Next page:` as a full command and ends the run, job and
model views with copyable `Next:` commands from the one follow-up renderer.

**Key order.** Every JSON or JSONL stdout line, including the
account, organization-list and package envelopes and every
runner error, is canonical: compact, with object keys sorted recursively as
strings at every depth (so `"10"` sorts before `"2"`), UTF-8 without ASCII
escaping, and one LF per document. Both products print identical bytes for
identical documents; Rust gets this from `serde_json::Value`, Bun from
`output.canonicalJson`. `--output-file` JSON uses the same order with a two-space indent. The one exception is
`cli service operations --json`, whose result is the public projection of
the embedded manifest.
The shared corpora compare stdout bytes, not only parsed values.
Share URLs, Checkout URLs, invitation tokens, webhook endpoints and signing
secrets appear only in the result of the operation that creates them (a
webhook's `endpoint_url` also appears in `job show` when its endpoint is the
secret-free job-id path). Output
contains server-provided prices only and no field matching `/cost/i`.

**Allowlisted output.** Every result is built by copying the fields its closed
schema (`shared/schemas/service/`) lists; nothing a service record carries
beyond them reaches stdout or stderr, in any output mode. The shared corpus
reruns every case with an unknown member on every service object and requires
identical output. Job output uses the user noun and one casing (`job`, `jobs`,
`max_jobs`, `job_limit`, snake_case fields, the opaque `revision_token`, run
counts in the states `queued`, `running`, `completed`, `failed`, `cancelled`,
`awaiting_billing`). A run's `billing_status` is `settled`, `settling` (a
charge not yet final) or `unknown`. Stream status text names the model
(`Running on MODEL`) and activity carries no tool call reference. A run's
error text prints mapped words (`shared/fixtures/service/run-errors.v1.json`)
with a generic fallback, never the service's stage names; `PROSE_DEBUG=1`
prints the service's text instead. An example that `example show` cannot
fetch is `viewOnWeb` with its `webUrl` when the service names one. Organization
members are numbered (`member N`, by when they joined, with a `handle` when
the service sends one); account and organization ids are not printed, and
`org member role|remove` take `N`, `member N` or an account id. Cursors
(`nextBefore`) are opaque.

**Errors and exits.** A service response is classified in this order:

1. its body `code`;
2. then a route override: `/repos` 401 and `PUT /programs/{slug}` 403 map to
   `GITHUB_LINK_REQUIRED`. `program save` narrows the 403 by the service
   `error` text: only the linked-login refusal keeps `GITHUB_LINK_REQUIRED`,
   a reserved handle or reassigned slug is `SERVICE_REQUEST_REJECTED`, and any
   other 403 (such as `Invalid API key.`) is `SERVICE_AUTH_REQUIRED`;
3. then its HTTP status, using the manifest's `errorClassification` table.

`details` carries `serviceStatus` and an allowlisted `serviceCode`. For 400,
409 and 422 on service operations only (not the account operations), it also carries `serviceMessage`: the
service `error` text after the account text validator, at most 512 code
points, with no control characters and with the key redacted. Account
operations keep their frozen mapping and emit no remote text. Exits follow
§7.5. The additional taxonomy codes are:

- `CONFIRMATION_REQUIRED` (2);
- service errors (10);
- `SERVICE_WATCH_DEADLINE` and `HOSTED_RUN_DETACHED` (21, not retryable: the
  run continues, so both carry `details.resumable: true` and
  `details.resumeArgv`; retrying the command would start a second run);
- `HOSTED_RUN_FAILED` and `RUN_SUBMISSION_AMBIGUOUS` (22);
- `HOSTED_RUN_CANCELLED` (24, not retryable).

A hosted cancellation reports the terminal `HOSTED_RUN_CANCELLED` (24).
Exit 30 stays reserved for structured semantic failure.

**Invocation errors and copyable commands.** Every
`INVOCATION_INVALID` of a service invocation carries an Action for its own
cause, never the generic runner syntax advice. When the Action names one
command, `details.suggestedArgv` holds its arguments after the product name,
and the Action shows the same argv as a shell-quoted `prose ...` line. The
suggestion is one of:

- a corrected copy of the rejected argv (a did-you-mean noun, verb or option,
  a dropped duplicate or extra token, `--dry-run` replaced by `--preview`, a
  global option moved before `cli`);
- a follow-up command: the listing that shows valid values for a missing
  argument (`run list` for `<RUN_ID>`, `program list` for a program, and so
  on), or the command's `--help`.

Follow-up commands, `resumeArgv`, `cancelArgv`, and every `prose cli ...`
command quoted in a reason or human hint come from one renderer. It writes
`--output <mode>` when the resolved mode is `json` or `jsonl`, then `cli` and
the words. A copied command therefore never switches to human output. The
`--output` and `--json` tokens that follow a rejected token are still read to
choose the output mode and the suggested commands. The phrase "place global
options before cli" appears only when a global option right after `cli`
(`cli --output json run list`) was rejected. A misspelled service noun, a bare
or unknown verb and a global option after `cli` are service invocation errors.
They render as `OpenProse: INVOCATION_INVALID: ...` in human mode, or as a bare
runner error in JSON, and they never name the executable path. Public builds
print no banner; a developer build prints its custom-endpoint label at most
once, before the first streamed or result byte. A failure that streamed
nothing prints only the error, whose first line already carries the label.
Both products render these bytes identically. The account `auth`,
`package` and `org list` grammars keep their runner rendering for their own
argument errors; an unknown verb in those groups is an intent-inference error
(below).

**Missing `cli`.** Service words given without `cli`
are an invocation error. They are never forwarded to the language runner and
never billed. After the runner globals, the runner checks the first
unconsumed tokens:

- **Command words.** Two words that form a command-path prefix of the
  manifest or the runner commands (`run submit`, `job list`,
  `org member list`, `harness list`), or one word that is a complete one-word
  path (`doctor`), are a service command. So is a lone word whose
  `nounSynonyms` entry names exactly one command: a complete path, or a
  group with one verb, which is appended (`login` → `auth login`, `whoami` →
  `auth status`, `models` → `model list`, `money` → `wallet balance`); a
  group synonym followed by one of its verbs names that command. A verb before
  its group (`list jobs` → `job list`) and a `commandRewrites` phrase (`stop`
  → `run cancel`, `delete` → `program delete`, `share` → `run share`,
  `cron` → `job create`, `show` → `run list --limit 1`) are service words
  too; a rewrite's explanation follows the suggested command in the reason.
  Distance is never used before `cli`. When neither word names an
  existing file or directory (resolved against `--cwd` or the process working
  directory), the result is `INVOCATION_INVALID` (exit 2) rendered by the
  service renderer. The reason reads ``<words> is a service command; did you
  mean `prose ... cli ...`? Nothing was forwarded or sent. To pass these words
  to the OpenProse language instead, put `--` before them`` (``is not a
  command`` for a synonym). The Action reads ``Insert cli before the service
  command: `prose ... cli ...`.`` (``Use `cli <command>`: ...`` for a
  synonym). `details.suggestedArgv` is the original argv with `cli` and the
  command words in place of the typed words, with every global kept in place.
- **Language commands.** `grammar.intentInference.languageCommands` is the
  SPEC 7.1 list (a contract test compares them). A language command that also
  names a service command (`status` → `service triage`, `help [COMMAND]` →
  `--help`, `examples` → `example list`) is forwarded when a local harness is
  selected. Under the default `openprose` harness, which runs no language
  command, it is the `INVOCATION_INVALID` rejection instead (reason
  ``<words> is a language command, which runs only with a local harness``).
  `run FILE` is always forwarded: its `HOSTED_UNAVAILABLE` refusal keeps the
  frozen Action, and `details.suggestedArgv` is `prose ... cli run submit FILE
  --preview`.
- **A file of that name.** When one of the words names an existing file or
  directory, the argv is a language command and is forwarded unchanged. If
  the default `openprose` harness then refuses with `HOSTED_UNAVAILABLE`, the
  Action gains ``If you meant the hosted service command, use `prose cli
  ...`.`` and `details.suggestedArgv` carries that argv.
- **Global aliases before `cli`.** An `optionAliases` entry whose target is a
  `grammar.globalOptions` name (`--format` → `--output`) may appear in the
  global position. When `cli` or service command words follow, the alias is
  suggested and never applied. Without `cli` or command words after it, the
  token stays opaque language input. Any other unknown option before `cli` is
  an ordinary unknown-option error.
- **Command options before `cli`.** A command-local
  option in the global position (a `commonOptions` flag such as `--json`,
  `--yes` or `--preview`, any operation option such as `--limit` or
  `--output-file`, or an `optionAliases` spelling of one such as `-j` or
  `-y`), followed by `cli` with only such options and runner globals in
  between, is `INVOCATION_INVALID` rendered by the service renderer. The
  reason reads ``<`flag`[ (canonical)]...> belongs after the command path, not
  before `cli`; nothing was forwarded or sent``; the Action reads ``Move
  <canonical names> after the command path: `prose ...`.``;
  `details.suggestedArgv` keeps the runner globals in place and appends the
  options, by canonical name and with their values, after the command path
  (before a literal `--`). `--json` among them selects JSON output for the
  error.
- **Pre-parse global values.** Before a manifest
  command, an `--output` value that is not a mode suggests the mode equal in
  letter case or nearest by distance, else `json`. This is a
  service-renderer error in both ports and never names the executable path.
- `prose -- <WORDS>` always forwards, with no hint.

`prose --help` (`runner-help.txt`) lists every manifest command path and the
exit-code dictionary (every exit code in `shared/errors/taxonomy.v1.json`).
It also lists the service environment variables and a
"For agents" section naming `prose cli service capabilities --json`,
`prose cli service triage --json`,
`prose cli service status --json`, `prose cli service operations --json`,
`prose cli service guide` and `details.suggestedArgv`.
`ci/render_service_help.py --check` (the `service-help` gate) fails when a
manifest command, an exit code or one of those entries is missing. It also
fails when the help names a command that does not exist. The shared case
`framework/help-runner-top-level` pins the bytes in both ports.

**Intent inference.** A word or option the grammar
does not know is never guessed into execution: it is `INVOCATION_INVALID`
(exit 2) with the corrected argv in `details.suggestedArgv`, even for reads.
The tables live in the manifest at `grammar.intentInference`, and both
products read them:

- `verbSynonyms` maps a word in verb position to candidate verbs in
  preference order; the first candidate that the group has wins (`run ls` →
  `run list`, `run get` → `run show`, `wallet get` → `wallet balance`,
  `org member rm` → `org member remove`). `nounSynonyms` maps a word right
  after `cli` to command words (`organization` → `org`, `whoami` →
  `auth status`). `optionAliases` maps a foreign option spelling to candidate
  options; the first one the operation accepts wins (`-y` → `--yes`,
  `-j` → `--json`, `--format` → `--output`, `--dry-run` → `--preview`). A synonym is tried
  only when the word is not itself a command or option in that position.
- Otherwise the suggestion is the unique nearest known word by
  optimal-string-alignment distance, in which an adjacent transposition is
  one edit (`lsit` → `list`, `shwo` → `show`, `--jsno` → `--json`). The
  limit is `distance.shortWordMax` (1) for words of up to
  `distance.shortWordLength` (4) characters and `distance.max` (2) beyond.
  A tie at the smallest distance suggests nothing.
- The reason reads ``unknown command `cli <typed>`; did you mean `cli
  <meant>`? Commands: <list>`` (or ``unknown command `cli <typed>`. Commands:
  <list>`` without a match). After `cli`, the list names every first word of
  a manifest command and the runner commands `doctor`, `harness`, `cleanup`
  and `config`. In a group it names the group's verbs, including the runner
  groups `harness`, `cleanup` and `config` and the account groups `auth` and `package`. An unknown
  option reads ``unknown option <typed> for `cli <command>`; did you mean
  <meant>?``. Every such error is rendered by the service renderer, so both
  products print the same bytes and never name the executable path.
- Suggestions are complete: `details.suggestedArgv`,
  run as given with `details.suggestedStdin` (when present) on standard
  input, is never itself `INVOCATION_INVALID`. A corrected noun or verb that
  leaves a group without a verb gains the next typed verb, the verb a
  misspelled next word means, or the group's only verb (`cli models` and
  `cli model` → `cli model list`); a group with several verbs suggests its
  `--help`. A corrected bare command whose arguments are all optional
  suggests its `--help` (`cli run submt` → `cli run submit --help`). When the
  corrected argv names a service command the grammar would still reject,
  the correction is settled: the Action reads ``<first fix>, then <second
  fix>`` and `suggestedArgv` is the second fix (`cli run shw` → `cli run
  list`, the listing for the missing RUN_ID). `optionConversions` change
  units: `--amount 5` (dollars, before or after `cli`) suggests
  `--amount-cents 500` and says the dollars were converted; a value that is
  not a positive whole number is not converted, the reason states the unit,
  and the suggestion is the help. `commandRewrites` map a phrase to a
  command plus options (`cli run result RUN_ID` → `cli run show RUN_ID
  --file outputs/result.json`; without RUN_ID, `cli run show --help`). An
  unexpected argument fills the one missing required value option
  (`cli wallet topup 500` → `--amount-cents 500`, `cli job create spec.json`
  → `--spec-file spec.json` when the file exists; an `N` option takes only
  digits). The argument of an operation with a `secretFileOptions` option
  (`cli wallet redeem CODE`, `cli org invitation accept TOKEN`) is presumed
  to be the secret: the reason and Action never echo it, and the suggestion
  reads the option from standard input (`--code-file -`) with a
  ``printf '%s\n' "$CODE" | ...`` example. An unknown option of a
  `--spec-file` operation that names a text, interval or enum key of its
  `spec` (`cli job update ID --name x`) suggests `--spec-file -`, and
  `details.suggestedStdin` holds the JSON object of every such option
  (`{"name":"x"}` and a newline); `*_secret` keys are never moved. A
  whole-number `--limit` out of range suggests the nearest accepted value
  (`--limit 500` → `--limit 200`, `--limit 0` → `--limit 1`). A did-you-mean
  in a handler's reason carries the corrected command (`VISIBILITY pubic`
  → `public`), and an existing `run download` directory suggests the first
  free `DIR-2` … `DIR-99` as `--output-dir`. The service corpus runner reruns
  every JSON case's `suggestedArgv` offline in the case's fixture and fails
  on `INVOCATION_INVALID` (`service_operations.py --round-trip-only` runs
  that check alone).
- Discovery: every `cli ... --help` topic has an
  `Examples:` section rendered from the manifest `examples` (an operation's
  own; one per child command, in command order, for a group), after the
  options or command list and before the global options or `Output:` line.
  An option whose value the client or service fills in when it is omitted
  carries a manifest `default`, printed as `Default: ...` (`wallet usage
  --start` 30 days ago and `--end` today, UTC; `run submit --model`,
  `--environment`, `--runtime`); `program save` says a new program is
  private. `prose --help` starts its "For agents" section within its first
  25 lines, before the runner commands and options.
  `render_service_help.py --check` fails when a topic lacks its examples, or
  when "For agents" moves below line 25.
- Help: `-h` is `--help` wherever `--help` is accepted. Bare `prose cli`,
  `prose cli help`, `prose cli help <command...>` and a trailing `help` or
  `-h` after a command group or path print that topic and exit 0, in every
  output mode.

**Runs.** `run submit` is always live. It mints a session UUID and records
`{session, runId, createdAt, lastSequence, sourceSha256}` in the run journal
before sending `POST /run?live=1&session=<id>` with `X-Session-Id` and
`Accept: text/event-stream`. The journal lives at
`$XDG_STATE_HOME/openprose/cli/production/runs/`, with directories 0700,
files 0600 and no key. The CLI never sends a non-live submission. It never
retries `POST /run` blindly. It resubmits at most once with the same session,
and only when the connection dropped before the first event. A 409 reporting
that the session already has a run proves the first submission was admitted.
The CLI then takes the run id from an `X-Run-Id` header when present, and
otherwise exits `RUN_SUBMISSION_AMBIGUOUS` and keeps the journal entry. Only
the duplicate-session 409 counts (it names a run, or carries the service's
"This session already has a run." text without a `code`); any other 409 is
classified normally, or is `RUN_SUBMISSION_AMBIGUOUS` after a resubmission.
Interrupting `run submit` or `run watch`, or reaching `--wait`, detaches and
never cancels. The `--wait` default is 30 minutes and the maximum is 6 hours.
An interrupt or a lost stream exits `HOSTED_RUN_DETACHED` (21, not
retryable: retrying `run submit` would start a second run) and the deadline
exits `SERVICE_WATCH_DEADLINE` (21). Both carry `runId`, `afterSequence`,
`resumeArgv` and `cancelArgv`. A `--wait` that passes before the run id is
known keeps reading the stream until the id arrives, for at most the stream
connect timeout, then exits 21 naming the run. `RUN_SUBMISSION_AMBIGUOUS`
(22) is left for a submission lost without a run id; its
`details.resumeArgv` is the same `run submit` with `--session S --detach`.
`run submit --session S` of a session that already has a run (this
machine's journal, or the service's duplicate-session 409 naming the run
without a resubmission) exits 0 with that run and `result.reused: true`.
`run submit` and `run watch` results carry `run_id` beside `runId`, and a
detached (or reused) result's `run` is `{run_id, status}`. When the service closes a `run watch` stream
cleanly with nothing past `--after`, the run has already finished, and watch
replays once from 0 to report its outcome. Only `run cancel --yes` cancels, and it reports the wallet's
`reserved_*` beside `available_*`. `run watch`, `run input` and `run cancel`
take the session from the journal unless `--session` is given; with neither,
they read the run record first.

**Ended runs.** `run cancel` of a run the service
reports as ended (409 `This run has already ended.`) exits 0 with
`{runId, status: "already_ended", runStatus}`: `runStatus` is the run record's
status from `GET /runs/{id}`, or null when the record is not written yet
(404); the wallet is not read. `run input` refused with 409 `This run is no
longer accepting instructions.` stays `SERVICE_WRITE_CONFLICT` (not
retryable), with `details.reason`, `details.runId`, an Action saying not to
retry and `details.suggestedArgv` `cli run show RUN_ID`. `run watch` without
`--session` or a journal entry reads `GET /runs/{id}`: an ended record
(`completed`, `error`, `failed`, `timeout`, `cancelled`) is reported like a
replay (exit 0 with `session: null`, `afterSequence: 0`, `source: "record"`
and `run` projected from the record without `response`; `HOSTED_RUN_FAILED`
22 or `HOSTED_RUN_CANCELLED` 24 with `details.files` and `source`), and a 404
or another status is `INVOCATION_INVALID` whose reason says the live session
is in the submitting machine's journal, with `suggestedArgv` `cli run show
RUN_ID`. `run download` without `--output-dir` writes to `./RUN_ID` and
still refuses an existing directory.

**Not found and idempotent deletes.** Every operation
that addresses a resource by an argument has a manifest `notFound` entry
(`resource`, an `id` template over its arguments, an optional `list` command,
an optional `hint`, and `serviceCodes` overrides such as
`organization_not_found`). A `SERVICE_RESOURCE_NOT_FOUND` on that operation
carries `details.resource` `{kind, id}`, a `details.reason` naming the id
(``job <id> was not found``) unless the handler gave
a more specific one, `details.suggestedArgv` for the listing command, and the
Action ``Run `<list>` to find the right <kind>, then retry with it.`` (or
``Check the <kind> identifier named in Detail, then retry with the right one.``
when no command lists it). A 404 on one of a run's files names the file
(`kind` `file`, `id` `RUN_ID/PATH`, `suggestedArgv` `cli run show RUN_ID`). A
`SERVICE_REQUEST_REJECTED` Action names only the details present (``Correct the
request as details.serviceMessage describes, then retry.``); with none it
points at the operation's `--help`, also as `suggestedArgv`. Human output
never names a JSON field in an Action: the rejected Action names the printed
lines instead (``Correct the request as the Detail and Service message lines
above describe, then retry.``), and the catalog Actions of
`SERVICE_WATCH_DEADLINE`, `HOSTED_RUN_DETACHED`, `HOSTED_RUN_CANCELLED` and
`RUN_SUBMISSION_AMBIGUOUS` point at the command their `Detail:` line names.
JSON keeps the catalog Action. A confirmed
`program delete` or `job delete` whose DELETE is a 404 exits 0 with
`{..., deleted: true, alreadyAbsent: true}` (`already_absent` for a job):
the goal state holds, so a retry after a lost response is not a failure.

**Idempotence only where it is true.** `alreadyAbsent`
is reported only for a 404 on a well-formed identifier the caller owns:
- `job delete` requires `JOB_ID` to be a lowercase UUID
  (`8-4-4-4-12` hex digits, as `job list` prints it). Anything else, including
  a path-like value such as `..`, is `INVOCATION_INVALID` (exit 2) before any
  request, with a reason naming the value (and the lower-case form when that
  is a UUID), the Action ``List your jobs with `<cli job list>` and pass one of
  their ids.`` and `suggestedArgv` `cli job list`.
- An own-scope `OWNER/SLUG` (`program save|visibility|delete|revisions`,
  `result publish|unpublish`) whose `OWNER` cannot be confirmed as the caller
  (the caller's `GET /programs/{slug}/revisions` is a 404 or an empty list) is
  `INVOCATION_INVALID` (exit 2) before any write, `--preview` included: the
  reason says ``OWNER "X" is not confirmed as you`` and names the bare slug to
  pass, the Action names the bare slug and `cli program list`, and
  `suggestedArgv` is `cli program list`. That 404 is never mapped to
  `alreadyAbsent`. (A first `program save` keeps its own reason asking for the
  bare slug.) When `OWNER` is confirmed, the plan (`--preview` result and
  `CONFIRMATION_REQUIRED` `details.plannedRequest`) carries
  `owner: {handle, verified: true}` and human output prints
  `Owner: HANDLE (verified as you)`; a bare-`SLUG` plan has no `owner`.
- `run cancel` and `run input` without `--session` or a journal entry read
  `GET /runs/{id}` (the manifest's `run.read` request; `run input` gains it as
  request 1) before the confirmation gate. An ended record (`completed`,
  `error`, `failed`, `timeout`, `cancelled`) makes `cancel` exit 0 with
  `{runId, status: "already_ended", runStatus}` (nothing is POSTed, the wallet
  is not read, `--yes` is not needed because nothing changes) and makes
  `input` `SERVICE_WRITE_CONFLICT` (exit 10, not retryable) with
  `details.reason` naming the status, `details.runId`, `details.source:
  "record"`, the ended-run Action and `suggestedArgv` `cli run show RUN_ID`.
  A 404 or a status that is not ended is `INVOCATION_INVALID` whose Action
  names the verb (``Cancel the run from the machine that submitted it (its run
  journal holds the live session) or pass that session with --session UUID;
  nothing was cancelled. ...``, and the `input` equivalent), with
  `suggestedArgv` `cli run show RUN_ID`. Cancel is therefore idempotent from
  any machine.

**Registry errors.** A registry 404 is
`SERVICE_RESOURCE_NOT_FOUND` (exit 10, not retryable), never
`SERVICE_UNAVAILABLE`: fetch and withdraw name `{kind: package, id:
ORG/NAME@VERSION}` with `suggestedArgv` `cli package list ORG`; list and
publish name the organization with `cli org list`. 413 and 429 stay
`SERVICE_UNAVAILABLE`. An invalid `cli package <COMMAND>` invocation is
`INVOCATION_INVALID` in the `openprose.service-operation/1` envelope in both
ports, before any request, with a per-cause reason (missing `ORG` or
`ORG/NAME@VERSION`, an option the command does not take with the ones it does,
a duplicate or valueless option, a missing required option, a value that is not
a slug, a version, a cursor or a digest) and `suggestedArgv` `cli package
<COMMAND> --help`; a missing or unknown command is a bare `INVOCATION_INVALID`
naming the four commands. A human listing with no public packages prints
`No public packages in ORG.` Human registry errors use the service error
layout (`<label>: CODE: message`, `Detail:`, `Action:`).

**Program and result references.** The owner-scoped
verbs (`program save|visibility|delete|revisions`, `result publish|unpublish`)
take `SLUG` or `OWNER/SLUG` as `program list` prints it. `OWNER` is checked
before any other request against the owner named by `GET
/programs/{slug}/revisions` (the caller's own revisions, compared
case-insensitively; `revisions` checks its own response): another owner is
`INVOCATION_INVALID` ending `pass "SLUG"`; when the caller has no program
`SLUG`, a first `save` is `INVOCATION_INVALID` asking for the bare slug and the other verbs are refused too, because `OWNER` cannot be
confirmed. Slug errors describe the pattern in words
and name the lower-case slug when that is valid. `result list|show` accept a
bare own `SLUG` (owner from the same revisions request, sent with the key).
`program show` resolves a numeric `@N` (`1` to `999999999`, no leading zero)
of the caller's own program through the revisions list to its `rev_id`
(another owner or an unknown number is `INVOCATION_INVALID` naming the command
to use) and, in human mode, reports it on stderr; every other command that
takes `@REV` refuses `@N` naming `program show ...@N --json`. Every projected
program and revision carries `ref` (`OWNER/SLUG@rev_id`); human `list` and
`revisions` lines label `rev_id=`, `commit_id=` and `ref=`. Follow-up hints
name the confirmed owner or the bare slug, never an `OWNER` placeholder.
`result show` without an id (or with the id `latest`) reads the newest
publication. `example show NAME` matches an exact id, else an id or label
case-insensitively, and an unknown name names the unique nearest id (the
manifest's `intentInference.distance`) with that `example show` command as
`details.suggestedArgv`. `program visibility` lower-cases `VISIBILITY` and
names the nearest value on a typo. `org rename ORG --name NAME` equals the
positional `NAME`.

**Preview fidelity, job spec schemas and confirmation reasons.** `plannedRequest` gains an optional `summary`: the non-secret top-level
JSON body fields the manifest lists in `confirmation.summaryFields` (`type`,
`slug`, `name`, `program_ref` as `programRef`, `model`, `reasoning_effort`, the
sorted keys of `inputs` as `inputKeys`, `interval_seconds`, `delivery_mode`,
`visibility`, `role`, `amount_cents`), omitted when none is present; secrets,
program text and input values never appear. Human previews print it as
`Summary: field=value; ...` (cents also in dollars) and `CONFIRMATION_REQUIRED`
as `Planned summary:`; a plan with a quote prints `Hold: $X (flat hold,
independent of program and model)`. `run submit` and `program draft` check a
`--model` against `GET /models` (manifest request `when: "--model"`) before the
confirmation gate: a name that is neither offered nor the default is
`INVOCATION_INVALID` naming the three nearest offered
models, with `suggestedArgv` the same command using the nearest; a failed
lookup is advisory (the service decides) except an interrupt. `program draft`
quotes the flat run hold (`GET /run/quote`, advisory) into its plan. `org
create` checks the service slug rule (1-63 lowercase letters, digits or
interior hyphens; not `openprose`, `system`, `default`, `invitations` or
UUID-shaped) before any request, suggesting a derived slug as `suggestedArgv`
when one exists. `job create`, `job update` and `job configure` check the spec
file against the operation's manifest `spec` (a closed per-type schema: keys,
required keys, `pinnedRef`, `stringMap`, `interval`, enums, nested bindings) and
report every violation at once in `details.violations` (the reason numbers
them): unknown keys with the nearest accepted key (compared without case, `_`
and `-`), camelCase names with their snake_case request names, an unknown type with the nearest type,
an unpinned `program_ref` with the command that prints `ref`, non-string input
values. A webhook spec with no `program_ref` (`spec.unpaidWhen`) plans effect
`write` with no hold and sends no quote. Every confirm-class operation carries a
manifest `confirmReason` (the consequence, e.g. rotate-secret invalidates the
old secret now, detach stops the program, unpublish removes the public page):
the `--yes` help line reads `required because <confirmReason>.` and
`CONFIRMATION_REQUIRED` carries `details.reason` `--yes is required because
<confirmReason>`; `render_service_help.py --check` and the manifest schema
refuse a missing, misplaced or circular reason. `run quote` results carry
`holdBasis` and its human output says the hold is flat and the price is known
only after settlement.

**Transport.** Requests never follow redirects and are never retried, not even
GETs. Each operation has a transport class with bounded time and size:

- `account`: the account command limits.
- `control`: 1 MiB and 15 s.
- `listing`: 8 MiB and 30 s.
- `stream`: 30 s to connect and 45 s idle, with events of at most 1 MiB.
- `download`: streamed to a fresh directory, at most 64 MiB per file, 512 MiB
  per run and 10,000 files.

Exceeding a limit is `SERVICE_RESPONSE_TOO_LARGE`. Every service request,
including the account ones, sends `X-OpenProse-Client: cli/<version>+<rust|bun>`
and `User-Agent: prose-cli/<version>`, and carries no user or host data.

**Service origin.** Public builds (every release and every
default build) talk only to the production origin in the manifest
(`environments.production.origin`)
with `OPENPROSE_API_KEY` or the `org.openprose.cli.production` stored key. They
read no variable, option or configuration value that changes the origin.

A developer build (Rust cargo feature `dev-endpoint`, Bun build define
`PROSE_DEV_BUILD=true`; neither is on by default) additionally reads
`OPENPROSE_API_URL`, an `https` origin with no path, query, fragment or user
info. When it is set, that origin replaces production, the key is still
`OPENPROSE_API_KEY`, `cli auth login` stores the key under a credential-store
service scoped to that origin (`org.openprose.cli.custom-<first 16 hex digits
of the SHA-256 of the origin>`) so it never replaces the production key, the
run journal uses the same `custom-<digest>` directory, reports carry
`environment: "custom"`, and human output is labeled `OpenProse (custom
endpoint <origin>)`. The override code is compiled out of public builds; the
`public-surface` gate checks the release binaries. All OpenProse credential
variables are filtered from every harness environment. The strict
`rr_test_[0-9a-f]{32}` predicate always applies.

**Credential teaching and account options.** Service verbs and the account verbs share one credential
classifier. `SERVICE_AUTH_REQUIRED` for a key problem carries
`details.credentialSource` (`environment`, `store`, `none`),
`details.credentialVariable` and `details.credentialProblem` (`missing`,
`malformed`, `rejected`), and its Action follows the source: `Replace or unset
<VARIABLE>, then retry.` for an environment key, ``Run `<login>` again, or set
<VARIABLE>, then retry.`` for a stored key, and ``Set <VARIABLE> or run
`<login>`, then retry.`` for no key, where `<login>` keeps the output mode.
`CREDENTIAL_STORE_UNAVAILABLE` names the variable in its reason and Action
(``Set <VARIABLE> for this command, or unlock or configure the operating system
credential store, then retry.``) except for `auth logout`; on macOS, a key
saved by an earlier build that the keychain will not release without a prompt
has the Action ``Run the command in a desktop session and allow access in the
macOS keychain prompt once, or set <VARIABLE> for this command.``. The runner-error
schema admits exactly these Actions besides the taxonomy text. `cli auth
status` and `cli org list` report a malformed or rejected key (401 or 403 on
`GET /organizations`) as that `SERVICE_AUTH_REQUIRED`, never
`SERVICE_PROTOCOL_INVALID`; the error keeps `details.credentialSource`
(`environment` or `store`, the one source enum) on a key failure. `cli auth login`
and `cli auth logout` while the variable is set are `INVOCATION_INVALID` with
a reason and Action naming it and `credentialSource: environment`. Human
account output prints a failure only as the service error text (label,
`Detail:`, `Action:`) on stderr, never a status line; on success
`cli org list` prints `SLUG  ROLE  NAME` lines (`-` without a role).
`cli auth status|login|logout`, `cli org list` and `cli package ...` accept
`--output` (separate or `=` value) after the command path; the same value
before `cli` is accepted and a different one is `INVOCATION_INVALID`.

**Coverage.** `shared/service/service-interactions.v1.json` is the service's
interaction export, projected to the interactions the manifest maps and
the routes it sends, with its digests in `service-interactions.source.json`.
`conformance/runner/service_coverage.py --strict-mapping` requires every
vendored interaction to be mapped and every vendored route to be sent. It also
requires every request to match an exported route and auth, and confirmation
and effect to be at least the catalog's. Interactions the CLI does not offer
never enter this repository. The
CLI has no raw request passthrough. `--strict`, which
the `service-coverage` admission gate runs, adds a case floor: every service
operation needs at least one success case and one failure case in
`conformance/cases/service/`, which both products run in full.

**Tests.** The shared corpus under `conformance/cases/service/` is
authoritative. It runs through `conformance/runner/service_operations.py`
against test-seam builds only, using the extended fixture format in
`conformance/runner/service-fixture.schema.json`. That format adds:

- query and request-header assertions;
- SSE frame scripts;
- disconnects and idle stalls;
- response headers;
- raw and base64 bodies;
- a virtual clock, deterministic ids and a preset journal.

Release builds ignore `PROSE_TEST_SERVICE_FIXTURE`. Hermetic tests do not
establish deployed behavior.

**Public surface.** This repository is a public user client. The
`public-surface` gate (`ci/check_public_surface.py`) fails when a tracked file,
either port's release `--help` output, or the strings of the release Rust
binary contain internal service detail or developer-only surface: non-production
service hostnames, developer credential variables, internal flag, deployment or
runtime names, or real run ids. Its denylist is `ci/public_surface_denylist.py`;
named internal terms in it are matched by salted digest, never written out.
