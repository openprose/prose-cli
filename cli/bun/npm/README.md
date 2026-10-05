# Prose CLI npm launcher

The current kernel release candidate uses one registry identity,
`@openprose/prose-cli`, with exact platform-suffixed payload versions selected
through optional dependency aliases. Install the ordinary root version; do not
install a payload-only version directly. npm installs the Bun implementation;
Rust is available through separate standalone archives.

The launcher verifies source/cohort identity, exact root and payload versions,
platform/runtime admission and executable digests before spawning the binary.
No lifecycle script is required. Missing or altered payloads fail closed.
Historical packages retain their original layout and validation.

See [publication setup](../../../docs/cli-publication.md) and
[release preparation](../../../docs/cli-release-next.md).

## Historical layouts and launcher custody

The following documents the earlier package layouts. The same custody checks
apply to the alias layout above.

# npm distribution source

`bin/prose.js` is the plain-Node launcher template for
`@openprose/prose-cli`. Local packaging replaces its single cohort token with
the exact version, source revision, release channel, image identity, purpose,
and admitted-platform set. It selects an exact-version optional platform
package, directly spawns that package's Bun standalone, relays catchable
signals, and preserves its exit status. Node.js 22.22.3 is the minimum runtime;
the launcher fails clearly below that mechanically exercised boundary.

Before spawning, it resolves only the platform package nested under or hoisted
beside this exact meta-package installation. It requires the expected package
identity, version, cohort, admitted platform, Bun compile target and runtime
variant, executable path, and byte length, and rejects executable symlinks or
paths outside that package. The meta manifest binds the launcher bytes, and the
platform manifest binds the packaged executable's SHA-256 digest. The launcher
reauthenticates the manifests, its own bytes, the executable, and any Windows
sidecar immediately before spawn. This detects same-user post-install mutation;
it is an integrity check, not a privilege boundary. The startup hashing cost is
an intentional functional-alpha integrity tradeoff and remains
benchmark-visible.

Linux platform manifests declare a fixed minimum of glibc 2.34 and the measured
maximum GLIBC symbol version required by the packaged ELF. Before resolving or
spawning the executable, the Node launcher reads its own runtime glibc version
and refuses versions below 2.34 with an actionable diagnostic. Current Linux
execution evidence is limited to Ubuntu 22.04; other distributions and runtime
combinations remain unverified even when their reported glibc is new enough.

The Windows platform package additionally carries the exact sibling
`bin/openprose-windows-process-host.exe` bound into both product builds. The
launcher validates its closed manifest declaration, non-symlink regular-file
identity, location, and byte length before launching `prose.exe`; it never
launches the sidecar itself. Current packages record native Job Object release
admission as false, so carrying the verified helper does not claim Windows
runtime readiness.

The launcher performs no download, compilation, shell invocation, or lifecycle
script work. Platform packages are produced only for a binary supplied to the
local packager. Functional-alpha draft publication uses `echo-v0`; a full
semantic release remains gated by the canonical image and release evidence
described in `cli/protocol/STATUS.md`.

The user-facing meta package includes the exact
`cli/conformance/live-alpha/hello.prose.md` bytes as
`examples/hello.prose.md`. Standalone archives carry the same relative member;
the executable-only platform npm package does not duplicate it. With `echo-v0`,
running that command transports and echoes only the opaque
`prose run <path>` task argv. The runner does not open or read the packaged
example file, does not evaluate OpenProse semantics, and does not return the
contract's `Hello, world!` value.

Functional-alpha harness support is target-specific: `darwin-arm64` admits
Prime, OMP, Codex, and Claude; `darwin-x64` admits Codex; `linux-x64-gnu` admits
Codex and OMP; `linux-arm64-gnu` admits Codex. Windows is omitted from the
functional alpha.

Prime exact versions 0.7.0 and 0.8.1 are admitted, OMP is exactly 18.0.9,
Codex is exactly `0.149.0-alpha.4.1`, and Claude is exactly `2.1.243`. These
versions are the exact alpha allowlist. Repair Prime with
`curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1`,
which installs the official versioned prime-agent release tarball. Prime and OMP commands use the explicit
profiles `prime-harness-login` and `omp-harness-login` plus a fully-qualified
model such as `openai-codex/gpt-5.4`. That example is illustrative; replace it
with a fully-qualified provider/model exposed by your selected harness login.
Those profiles mean harness-managed login with unknown billing, not subscription
billing.

If an owned Prime daemon cannot be settled, the CLI recursively scrubs prompt,
image, task, credential, and cache files within bounded entry, byte, and depth
limits, unlinking symlinks without following them. The error contains an opaque
handle and fixed `cli cleanup prime <handle>` argument suffix. Set `PROSE` to
the exact downloaded launcher that reported the failure, then run `"$PROSE"
cli cleanup prime <handle>` with the same `TMPDIR`/platform temporary-root
environment as the failed invocation. It
authenticates only that private run marker and socket; it never searches for or
stops another Prime service, and an interrupted final removal remains retryable
with the same handle. Run `"$PROSE" --output json cli cleanup prime <handle>`
for its single machine-readable cleanup report.

Generated macOS guidance states that alpha executables are ad-hoc signed and
not notarized. It instructs users to verify the checksum before considering a
file-specific `xattr -d com.apple.quarantine` Gatekeeper repair.

x64 platform packages use Bun's baseline CPU targets:
`bun-darwin-x64-baseline` and `bun-linux-x64-baseline`. ARM64 packages use the
native `bun-darwin-arm64` and `bun-linux-arm64` targets. Package evidence binds
the exact compile target and `baseline` or `native` runtime variant; Windows
remains omitted from the functional alpha.
