# CLI reconnaissance — 2026-09-10

Read-only inspection of `<checkout>`; no CLI source edits or Git operations. Commands below distinguish verified readiness from proposed launch recipes.

## Product identity

- `~/.local/bin/prose` is **old npm 0.13.0**, an SDK-based CLI. Do not accidentally use it for the new two-implementation experiment.
- New Rust executable: `<checkout>/cli/rust/target/debug/prose`.
- New Bun executable: `<checkout>/cli/bun/dist/prose`. This standalone is what the new npm packaging distributes; it is not the old globally installed npm command.
- Both existing binaries report the test-only mock transport. Rust dry-run confirms `sentinel-v1`, not the default production placeholder. Build clean data-injected binaries into the new lab before semantic evaluation.

## Custom interpreter image: supported without CLI edits

There is no runtime image flag. The existing supported **build-time data replacement** accepts a Markdown image directory, its manifest/schema artifacts, bundle, and checksum. The packaging metadata belongs in the lab; the shipped language can remain entirely Markdown.

```sh
python3 <checkout>/cli/shared/image/bundle/image_bundle.py build IMAGE_DIR BUNDLE --checksum CHECKSUM

OPENPROSE_IMAGE_SOURCE_DIR=IMAGE_DIR \
OPENPROSE_IMAGE_BUNDLE=BUNDLE \
OPENPROSE_IMAGE_BUNDLE_CHECKSUM=CHECKSUM \
CARGO_TARGET_DIR=<lab>/build/rust \
cargo build --manifest-path <checkout>/cli/rust/Cargo.toml --locked --offline -p prose-cli

bun <checkout>/cli/bun/scripts/image-bundle.ts build \
  --image-dir IMAGE_DIR --bundle BUNDLE --checksum CHECKSUM \
  --outfile <lab>/build/prose-bun
```

Use absolute substituted paths. Do not pass `--test-seams` or require release eligibility for an unvalidated image. The supplied image manifest must hash each Markdown payload, aggregate path/length/NUL/raw bytes, and exact ordered concatenation. `cli/shared/image/echo-v0/manifest.json` is a structural example, not language content to retain. The input task envelope has `schema`, `argv`, and `interactionMode: non-interactive`. A final minified JSON line must match the image-owned terminal schema, include its schema identity and string `semanticStatus`; a required `task.argv` field must exactly match the input. Do not falsely keep placeholder semantic status for real evaluation. Semantic correctness still requires independent artifact checks.

## Installed readiness (observed in both products)

| Harness | Installed | CLI admission | Useful scope |
|---|---|---|---|
| Prime | 0.7.0, `/opt/homebrew/bin/prime-agent` | admitted | Inline no-tool interpretation through CLI; direct execution can use tools |
| Claude | 2.1.257, `~/.local/bin/claude` | rejected: exact 2.1.243 required | Direct current CLI, or privately install pinned version |
| Codex | 0.153.4, app-bundled executable | rejected: exact 0.149.0-alpha.4.1 required | Direct current CLI, or privately install pinned version |
| OMP | absent | unavailable; installed Bun 1.3.5 below OMP's 1.3.14 requirement | Not first lane |

The Prime adapter admits a **single no-tool lifecycle**; OMP explicitly disables tools. These are transport limitations, not evidence that a Markdown interpreter cannot use tools. Claude retains native tools. Avoid rewriting the language to satisfy a no-tool transport when the program needs filesystem access.

## Launch recipes

Verified provider-free ready Prime selection:

```sh
PROSE --harness prime --model prime-inference/openai/MODEL \
  --auth-profile prime-harness-login --cwd FRESH_CASE_DIRECTORY \
  --timeout 45s --output json --dry-run run CASE
```

Remove `--dry-run` for the real call. All runner options precede `run`. The stored harness-login route reports unknown authentication until execution. Historical route evidence also names `prime-inference/anthropic/claude-haiku-4.5`; current availability is unverified. Explicit provider/model must remain part of each evidence record.

For a standalone filesystem execution, current Claude help confirms `--safe-mode`, `--print`, `--no-session-persistence`, `--model`, and system-prompt injection. Follow the existing Claude recipe with the current executable:

```sh
claude --safe-mode --print --output-format stream-json --verbose \
  --no-session-persistence --append-system-prompt-file INTERPRETER_MD \
  --model haiku 'Read the program at PROGRAM_PATH and execute it.'
```

Run in a fresh directory with only the intended test copy and approved tools. This bypasses the wrapper version restriction, not permissions. Pin exact observed model identity in the result; aliases alone are weak evidence. `--bare` is not equivalent: it disables keychain/OAuth auth. Do not introduce it accidentally.

Direct Prime supports `--append-system-prompt`, `--cwd`, `--no-context-files`, `--no-skills`, `--no-extensions`, `--no-session`, `--provider`, `--model`, and `-p --mode json`. Pass prompt text as a subprocess argument, never shell interpolation. A fresh `--daemon-socket` is advisable: the default daemon currently fails before any model call with a supervisor registry ownership error. Do not shut down the user's shared daemon.

## Verification evidence

- 33 provider-free tests passed in 2.51 s: image bundle, echo image, and real-harness runner tests. Log: `recon-evidence/provider-free-tests.txt`.
- Rust and Bun harness lists agree on the versions above.
- Rust Prime dry-run: ready; system-append placement; authentication unknown; installed sentinel image.
- Direct Prime smoke failed at daemon startup, before model invocation. Classification: environment/harness lifecycle, not language failure. `recon-evidence/direct-prime-smoke.json` retains bounded metadata.
- Existing product real Prime smoke is retained separately in `recon-evidence/rust-prime-smoke.json`; inspect its status before counting it. This tests a sentinel, not the language.

This reconnaissance establishes launch paths and limitations. It does not claim language conformance, Prose Complete status, or release readiness.

## Follow-up: real tool execution and precise blocker

Private pinned Claude installation succeeded with `npm install --prefix <lab>/tools/cli-baseline --no-audit --no-fund @anthropic-ai/claude-code@2.1.243`. No global installation changed. Prepend that directory's `node_modules/.bin` to PATH for wrapper selection. Both image-injected products now exist:

- Bun: `lab/tools/cli-baseline/prose-bun`, SHA-256 `797948c1ac125c4e1d30bc5590107cbe4057e0745926a001558e5c5568b45a7f`.
- Rust: `lab/tools/cli-baseline/rust-target/debug/prose`, SHA-256 `0e1177d106d6be3166c0fe786b3e470eaec4472b87203d3cbd1f111d713e84b1`.
- CLI source revision: `48de5b7b01107638d897a9c9ebaacc6d667c7404`.
- Pointer image digest: `480776533740f6c9f65c223711594194a064fe75aead6e890c381d54b461f7ea`.

The pointer reads the current working directory's INTERPRETER.md, then the requested program. Its separate final-line framing schema distinguishes semantic-success and semantic-failed. This is packaging only; no interpreter text or CLI source was altered.

Both products report pinned Claude ready, but both real filesystem canaries fail with PROTOCOL_MALFORMED and terminate the child (exit 143), before terminal settlement. A direct invocation of the same pinned Claude, same pointer and same task **passes**: reported model `claude-haiku-4-5-20251001`, three native Read calls, and the generated unpredictable input value correctly returned. Evidence is under `lab/tools/cli-baseline/`.

The precise mismatch is native Claude telemetry. After `system/init`, pinned 2.1.243 emits `system/thinking_tokens` with session_id, uuid, numeric estimated_tokens and estimated_tokens_delta. Existing ClaudeProtocol rejects every subsequent system event. Therefore the failure is not caused by tool use or the language. Minimal proposed repair: validate and ignore this session-bound informational event in both parsers, add shared native-shape fixtures, retain rejection of unknown session/invalid counters, and leave completion semantics untouched. This is a basic transport compatibility repair, not language machinery. No repair made by reconnaissance.

## Owned CLI repository and working baseline

A separate private lab repository, checked out at `<lab-checkout>`. Baseline `9dc7101`, validated telemetry `b7d01b8`, Rust framing `e118b66`. Source import records current dirty-tree hashes; predecessor remains untouched. Decision branches preserve the two repair commits. Push required a per-command HTTP postBuffer adjustment; no global Git setting changed.

Working binaries built from this repository are `<lab-checkout>/build/prose-rust` and `prose-bun`. Prepend `<lab>/tools/cli-baseline/node_modules/.bin` to PATH to select pinned Claude and Codex.

```sh
PROSE --harness claude --model haiku --cwd CASE --timeout 90s --output jsonl run PROGRAM.md
PROSE --harness codex --model MODEL --cwd CASE --timeout 90s --output jsonl run PROGRAM.md
```

CASE contains README.md (kernel), PROGRAM.md, and input.txt with an independently generated random value. Both products/harnesses have now returned that value through real file reads. Current pointer v1 reads README.md; framing stays outside the language. Rust only accepts a structural JSON-schema subset, so semanticStatus uses `type: string`, not enum. A further observed Rust report limitation marks dynamic terminal status as not-applicable despite the correctly returned semantic-success terminal; do not use that derived field alone as semantic evidence.

`compatibility/baseline-results.json` retains the compact successful observations. The fixtures tested the earlier frozen kernel body from language commit 25f3f1c; changing the entry filename does not claim testing subsequent language changes. All these baseline observations used installed-login auth, not API-key credentials. Codex already offers `--auth-profile openai-api-key`; Claude's explicit API profile remains the next work item.
