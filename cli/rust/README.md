# OpenProse Rust runner

This standalone Rust 1.87 workspace contains the native `prose` outer runner.
It is an outer transport runner, not an OpenProse interpreter, and has no
dependency on the repository's language or kernel packages.

## Output and image selection

`--output-contract native` accepts the actual native harness terminal and
preserves final prose without requiring a model-authored JSON envelope.
Semantic status is `not-applicable`: validate program artifacts independently.
`image-envelope` remains the default and requires the image-declared terminal
envelope in addition to native completion. `--output human|json|jsonl` controls
rendering separately. See [native output](../../docs/native-output.md) and
[private native capture](../../docs/native-capture.md).

A build without image overrides still embeds the nonsemantic `echo-v0` image.
That is a packaging default, not a limit on the runner: production-shaped
builds can instead embed a verified language-owned image or minimal entry
pointer using the documented [image bundle configuration](../shared/image/bundle/README.md).
The configured image supplies instructions; choosing native output does not
replace an echo image with a language interpreter. No CLI source changes are
needed to swap image data. Native completion alone is not a release or
language-conformance claim.

## Local verification

```sh
cd cli/rust
cargo test --workspace --all-targets --features prose-cli/test-seams --locked --offline
cargo clippy --workspace --all-targets --features prose-cli/test-seams --locked -- -D warnings
cargo fmt --all -- --check
```

Build and inspect an installed harness without starting a model run:

```sh
cargo build --locked -p prose-cli
./target/debug/prose --output json cli harness list
./target/debug/prose --harness codex --dry-run --output json run example.prose.md
```

The functional alpha can run installed `prime-agent`, `omp`, `codex`, and
`claude` executables directly, plus the optional `prose-agents-sdk` harness,
without a shell or outer PTY. Prime and OMP
require an explicit fully qualified `provider/model` and credential route.
Their HOME-backed harness caches are available only through the explicit
`prime-harness-login` and `omp-harness-login` auth profiles; those routes pass
no provider credential environment variables. Every Prime/OMP environment-key
profile instead receives a fresh runner-owned mode-0700 config directory for
the complete child/service lifetime, so an ambient harness store cannot
silently outrank it. Prime/OMP preflight never runs model-list/help auth probes;
all profiles report auth readiness as unknown and actual execution is the first
auth authority. Every actual Prime child also receives the adapter-owned
`PRIME_AGENT_TELEMETRY=0` opt-out; an ambient conflicting value is overwritten,
the selected credential route is unchanged, and no other adapter receives it.
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
Nearby patches and prereleases are detected but refused rather than admitted by
range extrapolation. Machine errors include the detected identity, exact
allowlist, and repair command; human doctor/run output prints the same copyable
repair command.

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

Without `--harness`, the selected harness is always `openprose`. Until the
OpenProse-billed adapter exists, a language run fails with
`HOSTED_UNAVAILABLE` and exit code 10. It never falls back to a third-party
harness.

Builds without image overrides embed the release-eligible `echo-v0` Skill Runtime Image. It is
a functional-alpha placeholder: it asks the selected harness to echo the task
and produce a structurally verified terminal envelope, so successful runs have
semantic status `not-applicable`. It does not implement the OpenProse language.
Strict wrapper admission and a full semantic release still require the future
canonical, language-owned runtime image plus versioned real-harness evidence.

An ordinary `cargo build` reports the development profile with test seams
disabled. An ordinary `cargo build --release` reports the release profile with
test seams disabled. The deterministic mock and release-ineligible
`sentinel-v1` image are enabled only by the explicit development test build:

```sh
cargo build --locked -p prose-cli --features prose-cli/test-seams
```

The build rejects `--release` combined with `prose-cli/test-seams`. Release
workflows can set an exact validated prerelease version without editing Cargo
metadata:

```sh
OPENPROSE_BUILD_VERSION=0.1.0-alpha.1 \
OPENPROSE_REQUIRE_RELEASE_IMAGE=1 \
cargo build --release --locked -p prose-cli
```

## Developer endpoint build (OpenProse developers only)

Public builds talk only to the production OpenProse service; the service
origin is compiled in and no variable, option or configuration changes it.
OpenProse developers who need another deployment build with the
`dev-endpoint` cargo feature, which is off by default and never part of a
published build:

```sh
cargo build --release --locked -p prose-cli --features dev-endpoint
```

Only this build reads `OPENPROSE_API_URL`, an https origin such as
`https://host.example` (no path, query or credentials), and sends every
service command there. The key still comes from `OPENPROSE_API_KEY` or
`prose cli auth login`, but a login stores it under a credential-store entry
scoped to that origin (`org.openprose.cli.custom-<first 16 hex digits of the
origin's SHA-256>`), so it never replaces the production key. Human output
names the endpoint (`OpenProse (custom endpoint <origin>)`) and JSON reports
`"environment": "custom"`; run journals live under `custom-<digest>` beside
`production`. The Bun port's dev build follows the same contract. Exercise the
feature with:

```sh
cargo test --workspace --all-targets --features prose-cli/test-seams,prose-cli/dev-endpoint --locked --offline
```

The `rust-clippy-dev-endpoint`, `rust-tests-dev-endpoint` and
`rust-build-dev-endpoint` gates in `cli/ci/run_local.py` lint and test the
feature and build the developer binary itself (without the test seams).

A release binary built on your own machine embeds the build paths of its
source and of Cargo's registry, which include your home directory. Remap them
before you share a locally built binary, as the `public-surface` gate does:

```sh
RUSTFLAGS="--remap-path-prefix=$(cd ../.. && pwd -P)=/openprose-source --remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo-home" \
  cargo build --release --locked -p prose-cli --bin prose
```

The optional [native workspace profile](../../docs/native-profiles.md) exposes Claude’s ordinary workspace tools, including native delegation, with separate explicit directory access and tool permission rules. Existing defaults remain unchanged.

Generic SDK budgets and their separate inner/outer deadlines are documented in [SDK execution budgets](../../docs/sdk-budgets.md).
