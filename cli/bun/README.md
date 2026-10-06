# OpenProse CLI — Bun implementation

This directory is an independent Bun 1.3.5 workspace for the OpenProse outer
runner. It does not parse OpenProse programs. It verifies an opaque Skill
Runtime Image, preserves task argument boundaries, selects a harness adapter,
and emits the shared runner records.

This is the thin, noninteractive CLI wrapper. It never opens or controls a TUI.
An interactive harness can read the language directory directly, or use an
explicitly installed skill or entry image; it need not invoke this binary.
No particular skill installation is required. Prompts and language-facing
Markdown remain owned by the separate language/image layer.

## Output and image selection

`--output-contract native` accepts the actual native harness terminal and
preserves final prose without requiring a model-authored JSON envelope.
Semantic status is `not-applicable`: validate program artifacts independently.
Ordinary published-kernel builds default to `native`; explicit image and test
builds default to `image-envelope`, which additionally requires the image-declared
terminal envelope. `--output human|json|jsonl` controls
rendering separately. See [native output](../../docs/native-output.md) and
[private native capture](../../docs/native-capture.md).

A build without image overrides resolves and verifies the published kernel
when preparing a run through an installed harness. It also embeds `echo-v0`
for local diagnostics; that fixture is not a startup fallback. Help, version
and harness inventory do not retrieve the kernel. See
[kernel startup](../../docs/kernel-startup.md) for the acquisition policy and
[image bundle configuration](../shared/image/bundle/README.md) for fixed inputs.
Native completion alone does not establish contract fulfillment or language
conformance.

## Local development

```sh
cd cli/bun
bun install --frozen-lockfile
bun run check
./dist/prose --help
./dist/prose cli harness list
./dist/prose --harness codex --dry-run --output json run fixture.prose.md
```

Start these commands at the repository root. The final command requires an
installed supported Codex and may retrieve the public kernel, but makes no
model call. Its readiness report is not proof of authentication or fulfillment.

`bun run check` performs strict TypeScript checking, unit/integration/shared-
schema tests and ordinary standalone compilation. Standalone smoke assertions
are exercised by the test suite. The compile
command disables Bun's runtime `.env`, `bunfig.toml`, `tsconfig.json`, and
`package.json` autoloading. A test places hostile `.env` and `bunfig.toml`
files in the invoked working directory and proves that the standalone ignores
them.

`bun run build` is the ordinary local build, matching Rust's default: it enables
published-kernel startup, reports the `development` profile, and compiles with test seams off.
It writes `dist/prose`. `bun run build:test` is the explicit test-only build;
it creates a private temporary `sentinel-v1` bundle, enables test seams, writes
`dist/prose-test` by default, and removes the temporary bundle. The test-only
command refuses to overwrite `dist/prose`. `bun run build:release` reports the
release profile with published startup and test seams off. Its structural check
of the embedded diagnostic image does not qualify the kernel or authorize a release.

### Developer endpoint build (OpenProse developers only)

Every ordinary, test and release build talks only to the production OpenProse
service. `bun run build:dev` writes a separate `dist/prose-dev` compiled with
`PROSE_DEV_BUILD=true`; only that binary reads `OPENPROSE_API_URL`, an https
origin (no path, query or credentials) that replaces the service origin at run
time. The key is still `OPENPROSE_API_KEY`, but `cli auth login` stores it
under a credential-store entry scoped to that origin
(`org.openprose.cli.custom-<first 16 hex digits of sha256(origin)>`), so it
never overwrites a production login. Human output is labeled
`OpenProse (custom endpoint <origin>)` and JSON envelopes report
`"environment": "custom"`. The build refuses `--require-release-eligible` and
the default `dist/prose` outfile. Public builds define the switch as false, so
Bun removes the override code (`src/core/service/dev-endpoint.ts`) and the
variable name from the binary; `test/dev-endpoint.test.ts` checks both builds.

Ordinary builds can discover and run supported user-installed Prime, OMP, Codex, Claude, and Agents SDK
harnesses through direct argument-array subprocesses. The adapters preserve
the complete image/task boundary, use adapter-specific credential allowlists,
and, when `image-envelope` is explicitly selected, recover the image-owned terminal
envelope without a shell, outer PTY, or
harness fallback. Codex and Claude can use their installed login state. Prime
and OMP additionally require a fully qualified `provider/model` plus an
explicit auth profile. `prime-harness-login` and `omp-harness-login` preserve
the respective HOME-backed harness store while stripping provider credential
environment variables; readiness and the selected provider, account, and
billing identity remain unknown, and neither name is a subscription claim.
Provider-key profiles remain separate explicit choices and receive a fresh
runner-owned mode-0700 config directory for the complete child/service
lifetime, preventing ambient harness stores from outranking the chosen key.
Prime/OMP doctor, dry-run, and run preflight never invoke model-list/help auth
probes; every profile remains unknown until actual execution, with no fallback.
Every actual Prime child receives the adapter-owned
`PRIME_AGENT_TELEMETRY=0` opt-out, overriding ambient conflict without changing
credentials; OMP, Codex, and Claude never receive that control.
OMP's current recipe enables its native tools; it no longer passes `--no-tools`
or requires an empty inventory. It retains `--no-lsp`, disabled extensions,
skills/rules, and a final private config overlay disabling retry and the
listed MCP discovery providers. Before prompting, the runner obtains the
correlated `get_state` tool inventory and validates its names. Native tool
calls/results are correlated across turns. A nonterminal `agent_end` remains
an error; a valid terminal is distinct from program fulfillment. Prime also
supports its native tool lifecycle. No adapter silently switches harnesses or
credential routes. See the repository [runner guide](../../README.md) for
native output, SDK support, image selection, and environment profiles.
Functional-alpha admission is an exact audited allowlist: Prime `0.7.0` or
`0.8.1`, OMP `18.0.9`, Codex `0.149.0-alpha.4.1`, Claude `2.1.243`,
and the optional `prose-agents-sdk` harness `0.1.0` (currently macOS ARM64).
These are exact audited identities, not a claim of qualification for every
accepted version. Claude additionally permits stable versions at or above the
recipe minimum within its major version. Codex permits an explicit
`--codex-compatibility probe` attempt after required capabilities are observed;
the default `qualified` mode retains its exact qualified set. Other unknown
identities remain rejected with actionable diagnostics. See
[Codex compatibility](../../docs/codex-compatibility.md). The CLI build remains on its pinned Bun 1.3.5
toolchain; the separate upstream OMP package/source audit used Bun 1.3.14.

Prime runs use one private per-run daemon socket. If its owned service cannot
be settled, OpenProse recursively removes prompt, image, task, credential, and
cache artifacts within bounded entry, byte, and depth limits. Symlinks are
unlinked without being followed. It then reports an opaque cleanup handle and
the fixed `cli cleanup prime <handle>` argument suffix. Set `PROSE` to the exact
downloaded executable that reported the failure and run `"$PROSE" cli cleanup
prime <handle>`. Recovery accepts
only the original mode-0700, current-user directory and mode-0600 marker under
the process's same canonical temporary root, reauthenticates Prime's exact
socket/version handshake, and refuses symlinks, unexpected entries, changed
markers, or alternate roots. A private retry ticket keeps the same handle valid
if final directory removal is interrupted. Use `"$PROSE" --output json cli
cleanup prime <handle>` for the single closed machine report. If `TMPDIR` or the platform
temporary root changes before recovery, restore that environment first; the
command fails safely rather than searching other directories.

Standalone compilation selects one exact native target. Every x64 release
binary uses Bun's `baseline` runtime variant rather than the AVX2-oriented
standard target; ARM64 binaries use their exact native target. Unsupported
build hosts fail before producing a mislabeled artifact.

Published startup also applies to ordinary release-profile builds. A completed
explicit `echo-v0` invocation proves transport completion only: it
does not parse OpenProse, claim semantic success, or earn a strict-wrapper
claim. The default `openprose` identity still fails closed with
`HOSTED_UNAVAILABLE` until OpenProse-billed execution exists.

The deterministic `mock` and `fake-process` transports remain test-only. They
are admitted only by the explicit `bun run build:test` command, which carries
the release-ineligible sentinel image and enables test seams. Without image overrides, ordinary
`bun run build` and release-profile builds use published startup with test seams off
and refuse these transports before execution. Internal provider-free controls
cannot be enabled in a release build and never become a harness, language, or
billing fallback.

Human mode streams only adapter-parser-accepted assistant prose. It withholds
control-shaped object/array lines and any prefix that reproduces credential,
task, model, working-directory, invocation, or runtime-image values; after a
successful withheld response it prints a fixed safety notice. JSON and JSONL
output paths remain governed by their settled machine schemas.

Unix process supervision uses a best-effort original process group with
graceful-then-hard group termination. This is sufficient for the controlled
fixture whose descendants remain in that group, but it is not strict or
race-free descendant containment: a child can escape with `setsid()`, so Unix
results never set `cleanupVerified` to true. The Windows product now uses an
authenticated, exact-sibling native process-host sidecar with bounded streaming
and Job Object supervision. The integration and package shape are provider-free
tested, but the compiled release admission remains off until the exact packaged pair runs
successfully on native Windows and that evidence is retained.

## Packaging boundary

The default build produces `dist/prose`, a local Bun standalone using published
startup with an embedded diagnostic fixture. Explicit image arguments disable
published acquisition and select the verified bundle. Packaging creates platform-specific npm packages
and a small Node-compatible launcher in `@openprose/prose-cli`. Both products
embed the same exact prerelease SemVer and source revision through
`OPENPROSE_BUILD_VERSION` and `OPENPROSE_BUILD_COMMIT`. The launcher selects an
exact-version optional platform package, verifies the executable's byte length
and SHA-256, performs no download or postinstall work, forwards signals, and
preserves the binary exit status. Isolated install, integrity, and foreground
signal tests exercise the packed tarballs without a registry.

Build and package checks establish mechanical transport and integrity facts.
Current publication gates and release evidence are maintained in the
[release guide](../release/README.md); none establish contract fulfillment.

The Node interpreter used by installed-package conformance is deliberately not
copied away from its dynamic libraries. Its resolved installed executable is
byte-checked before and after each invocation, while the JavaScript launcher is
executed from a private verified snapshot with its original CommonJS package
context. The interpreter's dynamic/resource closure is not captured, and a
same-user mutation between the last check and process start remains possible;
that lane cannot provide release or strict-custody authority.

The optional [native workspace profile](../../docs/native-profiles.md) exposes Claude’s ordinary workspace tools, including native delegation, with separate explicit directory access and tool permission rules. Existing defaults remain unchanged.

Generic SDK budgets and their separate inner/outer deadlines are documented in [SDK execution budgets](../../docs/sdk-budgets.md).
