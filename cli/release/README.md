# Local release foundations

`../ci/package_local.py` consumes already-built Rust and Bun executables. It
creates byte-deterministic current-platform standalone archives from one fixed
set of input executable bytes, a plain-Node npm
meta package, the matching Bun platform package, and deterministic checksum,
dependency-inventory, SBOM, provenance, and release-manifest scaffolding.

By itself, local packaging is not an independently reproduced build:
`SOURCE_DATE_EPOCH`, canonical tar metadata, fixed permissions, and sorted
members make repeated packaging of the same inputs identical, but do not prove
compiler output reproducibility. The functional-alpha workflow adds that
proof boundary. On every target it checks out the same candidate commit into
two physically distinct source roots, builds both Rust and Bun once in each
root with the same pinned toolchain and source-path remapping, and packages
both cohorts in separate invocations. It admits the result only when canonical receipts
and every byte in both exact nine-file inventories match, then makes only
cohort A available to later admission and draft assembly. The compared bytes
include both standalone archives, the npm meta and platform tarballs, and all
five checksum/evidence files; standalone and npm-platform Bun executables are
also required to be byte-identical.

Release CI and admission pin Node.js 24.20.0, the current Node 24 LTS patch at
the time this policy was updated. The npm launcher's `>=22.22.3` engine is a
consumer syntax/runtime compatibility floor, not the CI security baseline;
Node 20 is neither.

The canonical npm version successor is recorded in
[`npm-registry-lineage.v1.json`](npm-registry-lineage.v1.json). The public
`@openprose/prose-cli` lineage already ends at stable `0.14.0`, so the first
new functional alpha is `0.15.0-alpha.1`, published only under the `alpha`
dist-tag if publication is separately authorized. `latest` must remain on the
existing stable during the alpha. The checked-in authority is an offline
admission input, not proof that the registry is unchanged; every future
publication must repeat the live registry checks in
[`MIGRATION_AND_ROLLBACK.md`](MIGRATION_AND_ROLLBACK.md).

Release-facing changes for this independent CLI train are recorded in the
[CLI changelog](../CHANGELOG.md).
The [functional-alpha readiness contract](ALPHA_READINESS.md) identifies the
separate candidate, promotion, and post-publication authorities. Protected
promotion is a separate operation through
[`openprose-cli-alpha-promote.yml`](../../.github/workflows/openprose-cli-alpha-promote.yml);
it consumes admitted bytes and does not make this local packager a publication
authority.

Validate a candidate locally with:

```sh
python3 cli/ci/check_registry_lineage.py \
  --authority cli/release/npm-registry-lineage.v1.json \
  --version 0.15.0-alpha.1
```

## Functional-alpha package

Use one exact prerelease SemVer for both product builds and the package. From
the repository root:

```bash
VERSION=0.15.0-alpha.1
SOURCE_REVISION=$(git rev-parse HEAD)
REPOSITORY_ROOT=$(git rev-parse --show-toplevel)
SOURCE_ROOT=$(pwd -P)
CARGO_HOME_ROOT=$(cd "${CARGO_HOME:-$HOME/.cargo}" && pwd -P)
PLATFORM_ID=
PLATFORM_ARCH=
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) PLATFORM_ARCH=linux-x64 ;;
  Linux:aarch64|Linux:arm64) PLATFORM_ARCH=linux-arm64 ;;
  Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;
  Darwin:x86_64) PLATFORM_ID=darwin-x64 ;;
  *) echo "unsupported functional-alpha platform" >&2; exit 1 ;;
esac
if [ -n "$PLATFORM_ARCH" ]; then
  GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null) || { echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1; }
  case "$GLIBC_VERSION" in
    "glibc "[0-9]*.[0-9]*) ;;
    *) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  GLIBC_NUMBER=${GLIBC_VERSION#glibc }
  case "$GLIBC_NUMBER" in
    *[!0-9.]*|.*|*.|*.*.*) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  PLATFORM_ID="$PLATFORM_ARCH-gnu"
fi

case "$PLATFORM_ID" in
  linux-x64-gnu) ADMISSION_TARGET=linux-x64 ;;
  linux-arm64-gnu) ADMISSION_TARGET=linux-arm64 ;;
  darwin-arm64) ADMISSION_TARGET=darwin-arm ;;
  darwin-x64) ADMISSION_TARGET=darwin-x64 ;;
  *) echo "unsupported functional-alpha platform" >&2; exit 1 ;;
esac

CARGO_INCREMENTAL=0 \
RUSTFLAGS="--remap-path-prefix=$SOURCE_ROOT=/openprose-source --remap-path-prefix=$CARGO_HOME_ROOT=/cargo-home" \
OPENPROSE_BUILD_VERSION="$VERSION" \
OPENPROSE_BUILD_COMMIT="$SOURCE_REVISION" \
OPENPROSE_REQUIRE_RELEASE_IMAGE=1 \
OPENPROSE_IMAGE_SOURCE_DIR="$REPOSITORY_ROOT/cli/shared/image/echo-v0" \
OPENPROSE_IMAGE_BUNDLE="$REPOSITORY_ROOT/cli/shared/image/embedded/current.bundle.bin" \
OPENPROSE_IMAGE_BUNDLE_CHECKSUM="$REPOSITORY_ROOT/cli/shared/image/embedded/current.bundle.sha256" \
  cargo build --manifest-path cli/rust/Cargo.toml --release --locked -p prose-cli --bin prose

OPENPROSE_BUILD_VERSION="$VERSION" \
OPENPROSE_BUILD_COMMIT="$SOURCE_REVISION" \
  bun --no-env-file --config=cli/bun/config/empty-bunfig.toml run \
  cli/bun/scripts/image-bundle.ts build \
  --image-dir cli/shared/image/echo-v0 \
  --bundle cli/shared/image/embedded/current.bundle.bin \
  --checksum cli/shared/image/embedded/current.bundle.sha256 \
  --outfile cli/bun/dist/prose \
  --require-release-eligible

PACKAGE_PLATFORM_ARGS=()
if [ "$(uname -s)" = Linux ]; then
  PACKAGE_PLATFORM_ARGS+=(--readelf "$(realpath "$(command -v readelf)")")
fi

python3 cli/ci/package_local.py \
  --mode alpha \
  --version "$VERSION" \
  --source-revision "$SOURCE_REVISION" \
  --source-date-epoch 0 \
  --rust-binary cli/rust/target/release/prose \
  --bun-binary cli/bun/dist/prose \
  --image-manifest cli/shared/image/echo-v0/manifest.json \
  "${PACKAGE_PLATFORM_ARGS[@]}" \
  --out /tmp/openprose-cli-alpha
```

`alpha` mode requires the exact `functional-alpha-placeholder` purpose and
release-profile binaries with no test seams. It rejects the sentinel and also
rejects a canonical-language image, so this temporary functional-alpha
packaging path cannot be confused with full release admission. The package
manifest remains non-authoritative: `releaseEligible` and
`publicationAuthorized` are both false.

On Linux, packaging additionally requires `readelf`, measures the maximum
GLIBC symbol version required by both release-profile ELFs, and fails if either
exceeds the fixed glibc 2.34 floor. The release manifest, npm platform manifest,
and each standalone README retain that floor and the measured per-binary
maximum. Current execution evidence is explicitly Ubuntu 22.04 only; the floor
does not claim broader distribution portability.

The output contains two standalone archives and two npm tarballs. Both
standalone archives contain the exact repository contract bytes at
`examples/hello.prose.md`; the npm meta package contains the same member. The
platform npm package remains executable-only because the shared meta package is
the user-facing package. Install the exact pair into a collision-safe,
versioned prefix, offline and without lifecycle scripts:

```sh
VERSION=0.15.0-alpha.1
PLATFORM_ID=
PLATFORM_ARCH=
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) PLATFORM_ARCH=linux-x64 ;;
  Linux:aarch64|Linux:arm64) PLATFORM_ARCH=linux-arm64 ;;
  Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;
  Darwin:x86_64) PLATFORM_ID=darwin-x64 ;;
  *) echo "unsupported functional-alpha platform" >&2; exit 1 ;;
esac
if [ -n "$PLATFORM_ARCH" ]; then
  GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null) || { echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1; }
  case "$GLIBC_VERSION" in
    "glibc "[0-9]*.[0-9]*) ;;
    *) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  GLIBC_NUMBER=${GLIBC_VERSION#glibc }
  case "$GLIBC_NUMBER" in
    *[!0-9.]*|.*|*.|*.*.*) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  PLATFORM_ID="$PLATFORM_ARCH-gnu"
fi

INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PACKAGE_DIR=/tmp/openprose-cli-alpha
npm install --global --offline --ignore-scripts --prefix "$INSTALL_PREFIX" \
  "$PACKAGE_DIR/openprose-prose-cli-$PLATFORM_ID-$VERSION.tgz" \
  "$PACKAGE_DIR/openprose-prose-cli-$VERSION.tgz"
```

The registry repair for that same exact prefix and version is:

```sh
VERSION=0.15.0-alpha.1
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PLATFORM_ID=
PLATFORM_ARCH=
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) PLATFORM_ARCH=linux-x64 ;;
  Linux:aarch64|Linux:arm64) PLATFORM_ARCH=linux-arm64 ;;
  Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;
  Darwin:x86_64) PLATFORM_ID=darwin-x64 ;;
  *) echo "unsupported functional-alpha platform" >&2; exit 1 ;;
esac
if [ -n "$PLATFORM_ARCH" ]; then
  GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null) || { echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1; }
  case "$GLIBC_VERSION" in
    "glibc "[0-9]*.[0-9]*) ;;
    *) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  GLIBC_NUMBER=${GLIBC_VERSION#glibc }
  case "$GLIBC_NUMBER" in
    *[!0-9.]*|.*|*.|*.*.*) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  PLATFORM_ID="$PLATFORM_ARCH-gnu"
fi

npm install --global --ignore-scripts --prefix "$INSTALL_PREFIX" \
  "@openprose/prose-cli-$PLATFORM_ID@$VERSION" \
  "@openprose/prose-cli@$VERSION"
```

For the first journey, install exact Codex version `0.149.0-alpha.4.1` and
complete Codex sign-in. Then invoke only the exact candidate and its packaged
example for a nonsemantic transport check:

The run command contacts the selected provider and may incur charges under the signed-in account. The CLI cannot determine the account or billing route.

```sh
: "${VERSION:?set VERSION to the exact functional-alpha version}"
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PROSE="$INSTALL_PREFIX/bin/prose"
EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
npm install --global @openai/codex@0.149.0-alpha.4.1
codex login
"$PROSE" cli harness list
"$PROSE" cli harness use codex
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

Codex and Claude use their frozen installed-login defaults when selected this
way. Prime and OMP instead require `--model <provider/model>` and
`--auth-profile <ID>` on `cli harness use <ID>`; the CLI validates and saves
that bundle once, after which `cli doctor` and `run` need no selection override.
Switching harnesses clears stale saved bundle members atomically.

Upgrade by installing a chosen exact newer version beside this prefix. Remove
only this version with:

```sh
VERSION=0.15.0-alpha.1
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PLATFORM_ID=
PLATFORM_ARCH=
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) PLATFORM_ARCH=linux-x64 ;;
  Linux:aarch64|Linux:arm64) PLATFORM_ARCH=linux-arm64 ;;
  Darwin:arm64) PLATFORM_ID=darwin-arm64 ;;
  Darwin:x86_64) PLATFORM_ID=darwin-x64 ;;
  *) echo "unsupported functional-alpha platform" >&2; exit 1 ;;
esac
if [ -n "$PLATFORM_ARCH" ]; then
  GLIBC_VERSION=$(getconf GNU_LIBC_VERSION 2>/dev/null) || { echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1; }
  case "$GLIBC_VERSION" in
    "glibc "[0-9]*.[0-9]*) ;;
    *) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  GLIBC_NUMBER=${GLIBC_VERSION#glibc }
  case "$GLIBC_NUMBER" in
    *[!0-9.]*|.*|*.|*.*.*) echo "unsupported functional-alpha platform: glibc could not be verified" >&2; exit 1 ;;
  esac
  PLATFORM_ID="$PLATFORM_ARCH-gnu"
fi

npm uninstall --global --prefix "$INSTALL_PREFIX" \
  @openprose/prose-cli "@openprose/prose-cli-$PLATFORM_ID"
```

Every standalone archive now carries a package-mode-specific `README.txt`
with exact checksum, extraction, installation, harness-selection, doctor, and
first-run commands. The npm meta package carries the equivalent `README.md`
for registry or two-tarball offline installation. Alpha admission rejects
packages whose embedded guidance omits the nonsemantic `echo-v0` boundary,
supported harnesses, checksum verification, or first-use journey.

Provider-free mechanical functional-alpha availability is deliberately
narrower than artifact availability. This table records which adapters the
frozen fixture and package-admission journey exercise on each target; it is not
a real-provider pass matrix:

| Platform | Mechanically admitted harness adapters |
| --- | --- |
| `darwin-arm64` | Prime, OMP, Codex, Claude |
| `darwin-x64` | Codex |
| `linux-x64-gnu` | Codex, OMP |
| `linux-arm64-gnu` | Codex |
| Windows | Omitted from the functional alpha |

The exact admitted Prime versions are `0.7.0` and `0.8.1`; the recommended repair
is `curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1`,
which is upstream installation convenience, not binary provenance. Verify the
installed harness against the applicable upstream release evidence. The exact
admitted OMP version is `18.0.9`, and it requires Bun `>=1.3.14`, repaired with
`npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9`. The OMP
recipe and functional-alpha manifest carry that prerequisite as a structured
record, and package admission rejects drift between them without contacting a
provider. Codex admits
exactly `0.149.0-alpha.4.1`, repaired with
`npm install --global @openai/codex@0.149.0-alpha.4.1`; Claude admits exactly
`2.1.243`, repaired with
`npm install --global @anthropic-ai/claude-code@2.1.243`. These versions are the exact alpha
allowlist.

Choose only a harness supported by the platform table. Each block below must
start with the exact install or repair command. Authentication is not automated;
complete the harness sign-in flow before running `cli doctor` or `run`. Use
`codex login` for Codex and `claude auth login` for Claude. For Prime, start the
installed `prime-agent` harness separately, complete its interactive sign-in or
configuration, and then exit it. For OMP, do the same with the installed `omp`
harness. OpenProse CLI never invokes or controls that TUI. Prime and OMP require
a fully qualified model identifier and their explicit harness-managed login
profiles.

Prime — macOS Apple silicon only:

The run command contacts the selected provider and may incur charges under the signed-in account. The CLI cannot determine the account or billing route.

```sh
: "${VERSION:?set VERSION to the exact functional-alpha version}"
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PROSE="$INSTALL_PREFIX/bin/prose"
EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1
"$PROSE" cli harness use prime --model openai-codex/gpt-5.4 \
  --auth-profile prime-harness-login
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

OMP — macOS Apple silicon or Linux x64 only:

The run command contacts the selected provider and may incur charges under the signed-in account. The CLI cannot determine the account or billing route.

```sh
: "${VERSION:?set VERSION to the exact functional-alpha version}"
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PROSE="$INSTALL_PREFIX/bin/prose"
EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9
"$PROSE" cli harness use omp --model openai-codex/gpt-5.4 \
  --auth-profile omp-harness-login
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

Codex — every supported functional-alpha platform:

The run command contacts the selected provider and may incur charges under the signed-in account. The CLI cannot determine the account or billing route.

```sh
: "${VERSION:?set VERSION to the exact functional-alpha version}"
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PROSE="$INSTALL_PREFIX/bin/prose"
EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
npm install --global @openai/codex@0.149.0-alpha.4.1
codex login
"$PROSE" cli harness use codex
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

Claude — macOS Apple silicon only:

The run command contacts the selected provider and may incur charges under the signed-in account. The CLI cannot determine the account or billing route.

```sh
: "${VERSION:?set VERSION to the exact functional-alpha version}"
INSTALL_PREFIX="$HOME/.local/openprose-cli-$VERSION"
PROSE="$INSTALL_PREFIX/bin/prose"
EXAMPLE="$INSTALL_PREFIX/lib/node_modules/@openprose/prose-cli/examples/hello.prose.md"
npm install --global @anthropic-ai/claude-code@2.1.243
claude auth login
"$PROSE" cli harness use claude
"$PROSE" cli doctor
"$PROSE" run "$EXAMPLE"
```

The example model is illustrative. Replace it with a fully qualified model
identifier supported by the selected harness and authentication route.

The Prime and OMP profiles select harness-managed login routes. The CLI does
not verify the provider, account, or billing route. These profiles do not
establish subscription billing.

The saved bundle is the default when no higher-precedence project or
environment setting is active.

The alpha package-admission lane independently reopens the closed package,
checks artifact/SBOM/provenance/dependency bindings, installs both archives and
the offline npm pair, verifies the exact packaged Hello World bytes, and runs
the target-supported provider-free journeys through the direct Rust archive,
direct Bun archive, and offline npm-global installation. Each isolated journey persists
its harness, binds the exact saved configuration bytes, verifies
release-profile readiness with `doctor`, then executes `echo-v0` through that
saved default without a harness or transport override. The frozen fixture
answers only the admitted target's harness protocols; Prime and OMP receive
only a fixture authentication route and fully qualified fixture model. The
`echo-v0` image asks the selected harness to echo the opaque
`prose run <path>` task argument vector. The human-output safety policy may
withhold task-bearing text. This remains explicitly nonsemantic. The runner
does not open or read the packaged example file, execute its contract, or
return its `Hello, world!` value.
Real provider smoke tests remain explicit, cost-acknowledged, and separate.

Evidence schema v5 and matrix schema v4 are the current format authorities. The
exact packages from source commit
`31d81c55c8c90a7358b1cd8c5a0ccba631290a83` completed a 12-cell Darwin
ARM64 collection on 2026-08-31: Rust, Bun, and npm each passed Prime, OMP,
Codex, and Claude. The pathless aggregate and scope report are checked in under
[`live-alpha/evidence`](../conformance/live-alpha/evidence/31d81c55c8c90a7358b1cd8c5a0ccba631290a83/REPORT.md).
Historical direct 8/8 and v2 12/12 observations remain history and were not
relabeled.

The current matrix binds target and candidate custody plus each harness's
bounded declared package/runtime byte closure, distribution route, model, and
auth-route category. It explicitly leaves ambient harness configuration,
plugins, skills, cached account/provider state, dynamic resources, and
provider-side routing external and unbound. It is one candidate-reported
transport-smoke record per cell, not a reliability sample; provider spend is
unverified and semantic status is `not-applicable`. It does not promote
`echo-v0`, close a full-release gate, or authorize publication.

A separate exploratory W54 run is also not packaged v5/v4 evidence. Rust
with Prime `0.8.1` completed `echo-v0` after the telemetry-off control was
added, while Bun with Prime failed twice with `PROTOCOL_MALFORMED` before
`agent_end`. Both Rust and Bun OMP environment-key runs failed with
`PROTOCOL_MALFORMED`. Rust Codex and Rust Claude completed, with Claude
requiring one explicitly authorized retry. These mixed results do not resolve
the malformed-protocol failures, evaluate OpenProse semantics, establish
platform portability, or authorize a release or publication.

Run that exact admission boundary with a full lowercase source commit:

```sh
python3 cli/ci/alpha_package_admission.py admit \
  --packages /tmp/openprose-cli-alpha \
  --work-root /tmp/openprose-cli-alpha-work \
  --out /tmp/openprose-cli-alpha-admission.json \
  --target-id "$ADMISSION_TARGET" \
  --version "$VERSION" \
  --source-sha "$SOURCE_REVISION" \
  --image-manifest cli/shared/image/echo-v0/manifest.json

python3 cli/ci/alpha_package_admission.py verify-report \
  --packages /tmp/openprose-cli-alpha \
  --report /tmp/openprose-cli-alpha-admission.json \
  --target-id "$ADMISSION_TARGET" \
  --version "$VERSION" \
  --source-sha "$SOURCE_REVISION" \
  --image-manifest cli/shared/image/echo-v0/manifest.json
```

Build from a clean checkout when the resulting report will be retained: the
source revision identifies committed source, so it must not imply that
uncommitted edits were covered.

## Functional-alpha draft workflow

`.github/workflows/openprose-cli-alpha-release.yml` is the intentionally narrow
draft-prerelease preparation path for `echo-v0`, designed for protected
operation. It must not be dispatched until the repository controls described
below are configured. A manual dispatch from `main` accepts only an
exact `X.Y.Z-alpha.N` version, a full lowercase commit on `main`, and the fixed
draft-only choice. The remote tag `cli-vX.Y.Z-alpha.N` must already exist and
peel to that exact commit. The workflow tests provider-free behavior, builds
four native POSIX targets, admits every downloaded archive and npm pair on its
target runner, revalidates the reports during assembly, and creates an
unpublished GitHub draft prerelease. It cannot publish npm, create a public
release, or claim that OpenProse semantics ran.

### Required functional-alpha tag order

Tag policy is an external repository control and must be configured before the
first alpha tag is created. Add a GitHub ruleset for `refs/tags/cli-v*` that
restricts creation to the release operators, restricts updates, and restricts
deletions, with bypass authority limited to the intended release maintainers.
Configure the required reviewers for the separate
`openprose-cli-alpha-release` environment as described below. The workflow
cannot establish or infer either repository setting.

After the candidate commit is final, merged into `main`, and selected by its
full lowercase SHA, move the release entries out of `[Unreleased]` and into the
exact dated section required by the CLI changelog before creating the version
tag. Commit that change, then have an authorized release operator create exactly
one annotated tag and verify its local peel:

```sh
VERSION=0.15.0-alpha.1
SOURCE_SHA=0123456789abcdef0123456789abcdef01234567
TAG="cli-v$VERSION"

git fetch origin main --tags
git merge-base --is-ancestor "$SOURCE_SHA" origin/main
if git show-ref --verify --quiet "refs/tags/$TAG"; then
  echo "tag already exists; inspect it instead of replacing it" >&2
  exit 1
fi
git tag --annotate "$TAG" "$SOURCE_SHA" \
  --message "OpenProse CLI $VERSION functional alpha"
test "$(git rev-parse "$TAG^{commit}")" = "$SOURCE_SHA"
```

The next command is an intentional remote mutation. Run it only from the
authorized release-operator context after the tag ruleset is active; local
preparation, tests, and the current implementation work must not run it:

```sh
git push origin "refs/tags/$TAG:refs/tags/$TAG"
test "$(git ls-remote origin "refs/tags/${TAG}^{}" | awk 'NR == 1 {print $1}')" = "$SOURCE_SHA"
```

Only after that remote peel is verified should the operator dispatch
`OpenProse CLI Functional Alpha Release` from the `main` branch, passing
`version=$VERSION`, `source_sha=$SOURCE_SHA`, and leaving `draft_only=true`.
The draft helper refuses a missing or differently resolved tag before creating
or resuming a release, never creates, moves, or deletes the tag, and peels it
again after reconciling the exact asset set. A concurrent tag deletion or move
therefore fails closed. The resulting GitHub object remains both `draft: true`
and `prerelease: true`; publishing that draft is a separate, currently
unauthorized action.

Both Linux jobs run on Ubuntu 22.04 and inspect the final Rust and Bun ELFs
before packaging. Draft assembly revalidates the fixed glibc 2.34 floor, both
measured ELF maxima, and the Ubuntu-22.04-only evidence label from each Linux
release manifest.

Each target package job produces two physically distinct source-root build and
package cohorts. A controller-owned verifier captures canonical receipts
that bind the source, image, runner, pinned tool bytes, and exact nine-file
inventory. It rejects non-comparable identities or any file-byte difference;
only the already-compared cohort A is uploaded. Thus the standalone archives,
npm meta/platform tarballs, and their five evidence files are all subject to
the same byte-for-byte reproducibility gate rather than merely deterministic
repackaging of one build.

On Linux, both receipts bind one exact direct `readelf` executable. Receipt
capture and final comparison reauthenticate it and use it to inspect the
packaged Rust standalone ELF, Bun standalone ELF, and Bun executable inside
the npm platform tarball. The verifier proves the standalone/npm Bun bytes are
identical and independently checks the recorded GLIBC maxima, fixed 2.34
floor, and Ubuntu-22.04-only claim against those exact ELFs before cohort A can
be uploaded.

GitHub has announced deprecation of its hosted Ubuntu 22.04 runner images. The
current labels therefore remain a time-bounded build-environment claim: before those
runners retire, move the Linux build and admission jobs into a pinned Ubuntu
22.04 container on a supported host while retaining the same glibc inspection
and exact-package execution checks.

On macOS, `alpha` and `release` packaging runs
`/usr/bin/codesign --verify --deep --strict` against the owned Rust and Bun
input snapshots and refuses either invalid result. Alpha admission repeats
that strict check after extraction for the Rust and Bun standalone installs
and for the Bun executable installed through npm. This verifies executable
integrity after each packaging surface; it is not Apple notarization,
Developer ID signing, or distribution identity authority.

The reproducibility receipts bind the exact `codesign` and `otool` tool bytes
used by the target job. After the two package inventories compare byte for
byte, the controller-owned verifier uses that exact `otool` to inspect the
packaged Rust standalone, Bun standalone, and npm-platform Bun Mach-O files,
requires the two Bun copies to match, and enforces the alpha macOS minimum-OS
ceiling. This complements the strict codesign checks; it does not replace them
or constitute notarization.

The generated first-run guidance therefore calls the executables ad-hoc signed
and unnotarized. The generated package README contains the one exact quarantine
command for its installed executable. After checksum verification, a user whose
downloaded binary is quarantined may inspect that exact file and follow that
package-owned command. This is not signing or notarization.

Generated Bun and npm macOS candidates declare Bun's macOS 13+ runtime floor;
that build/toolchain declaration is not retained compatibility evidence for
every macOS 13 minor release or hardware generation. The x64 artifacts
deliberately embed Bun's baseline CPU runtimes through
`bun-darwin-x64-baseline` and `bun-linux-x64-baseline`; ARM64 artifacts use the
native `bun-darwin-arm64` and `bun-linux-arm64` targets. The release manifest's
closed `bunRuntime` record and each npm platform manifest bind the exact compile
target and `baseline` or `native` runtime variant. The Rust standalone does not
inherit Bun's runtime floor.

The alpha workflow separates candidate execution from repository write
authority. Package/report revalidation and release-asset assembly run in a
read-only job and upload one closed `alpha-release-assets` artifact. Only the
draft job receives `contents: write`; before any use, that job must be protected
by the named environment and required reviewers. It checks out the workflow
controller rather than the candidate, downloads that closed
assembly, revalidates its regular-file inventory and hashes, and performs the
retry-safe draft reconciliation. The draft release body itself includes
artifact selection, checksum, standalone/npm installation, harness setup,
doctor, and `echo-v0` smoke-test instructions.

Before its first use, repository administrators must configure the
`openprose-cli-alpha-release` GitHub environment with the desired required
reviewers. That external repository setting is deliberately not inferred from
the workflow file.

## Development package

From the repository root on a supported POSIX platform, choose an output path
that does not exist. Ordinary development packages use the `echo-v0` image and
have test seams disabled. The explicit `build_local.py` test driver does not produce
package inputs from its sentinel/test-seam candidates. With `--package`, it
captures those test candidates for any requested provider-free smoke first,
then builds and snapshots a separate ordinary `echo-v0`/no-seam Rust and Bun
pair. Only that second pair reaches `package_local.py`:

```sh
python3 cli/ci/build_local.py \
  --smoke \
  --package /tmp/openprose-cli-artifacts \
  --json
```

The JSON report keeps the smoke candidates under `candidates` and records the
separate package identities under `package.inputs`, with
`package.inputBuild.testSeamsEnabled` set to `false`. The driver removes
provider credentials and ambient build overrides from both build phases.

The equivalent lower-level ordinary package-input commands are:

```sh
VERSION=0.1.0
SOURCE_REVISION=development
REPOSITORY_ROOT=$(git rev-parse --show-toplevel)

OPENPROSE_BUILD_VERSION="$VERSION" \
OPENPROSE_BUILD_COMMIT="$SOURCE_REVISION" \
OPENPROSE_IMAGE_SOURCE_DIR="$REPOSITORY_ROOT/cli/shared/image/echo-v0" \
OPENPROSE_IMAGE_BUNDLE="$REPOSITORY_ROOT/cli/shared/image/embedded/current.bundle.bin" \
OPENPROSE_IMAGE_BUNDLE_CHECKSUM="$REPOSITORY_ROOT/cli/shared/image/embedded/current.bundle.sha256" \
  cargo build --manifest-path cli/rust/Cargo.toml --locked -p prose-cli --bin prose

OPENPROSE_BUILD_VERSION="$VERSION" \
OPENPROSE_BUILD_COMMIT="$SOURCE_REVISION" \
  bun --no-env-file --config=cli/bun/config/empty-bunfig.toml run \
  cli/bun/scripts/image-bundle.ts build \
  --image-dir cli/shared/image/echo-v0 \
  --bundle cli/shared/image/embedded/current.bundle.bin \
  --checksum cli/shared/image/embedded/current.bundle.sha256 \
  --outfile cli/bun/dist/prose

set --
if [ "$(uname -s)" = Linux ]; then
  READELF=$(command -v readelf) || { echo "readelf is required" >&2; exit 1; }
  READELF=$(realpath "$READELF") || exit 1
  set -- --readelf "$READELF"
fi

python3 cli/ci/package_local.py \
  --mode development \
  --version "$VERSION" \
  --source-revision "$SOURCE_REVISION" \
  --source-date-epoch 0 \
  --rust-binary cli/rust/target/debug/prose \
  --bun-binary cli/bun/dist/prose \
  --image-manifest cli/shared/image/echo-v0/manifest.json \
  "$@" \
  --out /tmp/openprose-cli-artifacts
```

The output contains one Rust archive, one Bun archive, the npm meta package,
the current-platform npm package, `SHA256SUMS`, and four JSON evidence files.
The dependency report inventories all three resolved component graphs and is
bound into the manifest, SBOM, provenance, and checksum set. The packager
refuses to overwrite an existing output path.

On Windows, the local driver builds `openprose-windows-process-host.exe` first,
captures its exact SHA-256 digest, and compiles both products with that digest
and admission `0`. A direct caller of the lower-level packager must pass those
same bytes with `--windows-process-host`. Windows packaging refuses a missing,
empty, non-regular, or symlinked helper; snapshots it once; and places those
verified bytes beside both standalone executables and the npm platform
executable. The release manifest, SBOM, and provenance bind the same digest.
Non-Windows packaging rejects the Windows-only argument to prevent target confusion.

The two npm tarballs can be tested or installed together without lifecycle
scripts or a registry lookup. Bind the exact platform recorded by this local
package set before constructing either filename:

With `VERSION=0.1.0`, the development meta-package filename is exactly
`openprose-prose-cli-0.1.0.tgz`.

```sh
PACKAGE_DIR=/tmp/openprose-cli-artifacts
VERSION=0.1.0
PLATFORM_ID=$(
  python3 - "$PACKAGE_DIR/release-manifest.json" "$VERSION" <<'PY'
import json
import sys

manifest_path, expected_version = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as source:
    manifest = json.load(source)
platform_id = manifest.get("platform")
supported = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64-gnu",
    "linux-x64-gnu",
    "win32-x64",
}
if (
    manifest.get("mode") != "development"
    or manifest.get("version") != expected_version
    or platform_id not in supported
):
    raise SystemExit("local package manifest is not the expected development set")
print(platform_id)
PY
) || exit 1
npm install --global --offline --ignore-scripts \
  --prefix "$HOME/.local/openprose-cli-$VERSION" \
  "$PACKAGE_DIR/openprose-prose-cli-$PLATFORM_ID-$VERSION.tgz" \
  "$PACKAGE_DIR/openprose-prose-cli-$VERSION.tgz"
```

`--source-revision` must exactly match the commit identity reported by both
input binaries through each exact CLI executable's `cli doctor` operation.
Local default builds report `development`; release automation injects the
immutable source revision into both products before building. The packager
refuses mixed or mislabeled bytes.

Development mode requires ordinary no-seam executables and marks the package
set as release ineligible. The embedded `echo-v0` image is eligible only for
functional-alpha transport admission. These bytes are local test artifacts and
must not be published. Release mode fails before creating its
output directory unless the embedded-image manifest is release eligible and
explicit canonical-profile and release-evidence files are supplied. Their
digests are recorded, but the local packager does not claim to validate their
external authority. Consequently even `--mode release` produces a candidate
whose top-level `releaseEligible` and `publicationAuthorized` fields remain
false. Only a separately protected validator may emit a promotion attestation;
arbitrary nonempty local files cannot make a package set publishable.

A functional-alpha or eventual full-release candidate must be built with
`cargo build --release` and Bun's release build, with the same immutable
`OPENPROSE_BUILD_COMMIT` and exact `OPENPROSE_BUILD_VERSION` set for both
commands. Ordinary `bun run build` produces the development `echo-v0` profile
with test seams disabled and is never a release input. Only the explicit test
build includes provider-free conformance seams.

The evidence is deliberately narrow: lockfile-declared component integrity is
inventoried, but fetched package bytes are not independently verified; signing
and vulnerability review are reported as `not-performed`; license authority is
unknown; and ordinary local tests report network isolation as `not-enforced`.
A protected publication workflow must add those independent controls without
rebuilding promoted artifact bytes.

The non-publishing local rehearsal has successfully exercised one complete
internal mock-benchmark development package set: both standalone installs plus
the offline npm pair completed all 240 selected mechanical validations. The
rehearsal binds those installed bytes to the same sentinel/test-seam snapshots
that passed its smoke step. This purpose is available only to the rehearsal's
internal Python call; neither packaging command exposes it, ordinary
use of the driver's `--package` option still produces `echo-v0`/no-seam inputs,
and alpha or release mode cannot admit it. This is evidence for local packaging and
invocation mechanics only, not semantics, portable OpenProse behavior, strict
process containment, or release admission.

## Draft workflow

`.github/workflows/openprose-cli-draft-release.yml` is a manual, draft-only
build-once pipeline. It is deliberately unusable today: the protected producer
described below is absent, and even a correctly sourced authority artifact
could not make the current noncanonical `echo-v0` image pass full-release
preflight. Local preflight tests retain that refusal as a machine-readable
report before the five-platform build matrix. No tag-creation, push, npm/Cargo
publication, or promotion stage exists. The draft helper refuses unless the
version tag already exists and
peels to the exact source commit. GitHub's Releases API has no atomic
ref-SHA precondition, so that check is preflight race hardening rather than
proof that the tag cannot move; protected tag rules and publication-time
revalidation remain external release requirements.

Draft creation is retry-safe after a partially accepted upload: the helper
searches a bounded authenticated release inventory, resumes only one exact
matching draft, reuses only byte-identical uploaded assets, and reauthenticates
both the draft and peeled tag after reconciliation. It issues no automatic
delete, patch, publication, or tag-creation request.

The workflow also requires an immutable artifact from the separately protected
`.github/workflows/openprose-cli-protected-release-authority.yml` producer and
verifies its GitHub run, artifact, environment, provenance, source, and image
identity. That producer workflow is intentionally absent today. This is a
deliberate refusal boundary: neither candidate source nor dispatch-provided
files can self-author the canonical-profile or release-evidence pass.

When the external gates eventually exist, preflight will require the requested
version to match both products and bind the full source SHA, complete image
directory, exact embedded bundle/checksum, image digests, and the canonical-
profile/release-evidence attestations. Release control code is checked out
separately from `main`; only a full candidate SHA already ancestral to `main`
can proceed. The protected profile job also runs main-owned dependency evidence
code against that exact candidate checkout; each platform package must retain
those exact bytes. Later jobs only download, hash, verify, package, and
assemble the bytes produced by the one native build stage, retaining native
manifests and verification lineage. Executed verification copies are never
reuploaded as native authority: verification uploads one report, while package
and assembly jobs redownload the original immutable build artifacts and bind
their closed file inventories and digests to that report. The Windows lane
builds and hashes the closed process
host before either product and retains the exact sidecar throughout native
verification and packaging. Its Job Object release admission remains false;
including the sidecar is not a native-readiness claim. The draft job, which
must not run until its named environment is externally protected, is the only job with
`contents: write`; its closed HTTPS client submits literal `draft: true` and
uploads the aggregate `SHA256SUMS` file plus exactly the members declared by
that inventory, using the immutable bytes captured during admission. It cannot
make the candidate public.

After native verification and packaging, a five-target admission matrix runs
code from the control checkout against the separately downloaded original package
artifact. POSIX targets install both release archives plus the offline npm
meta/platform pair and exercise eight release-safe, provider-free invariants on
the Rust, Bun, and npm surfaces. Windows validates the closed package,
executables, npm launcher, and sidecar lineage without spawning a candidate.
The canonical reports explicitly deny semantics, portability, ranking, strict
containment, release eligibility, and publication authority. Assembly and the
draft helper independently bind those reports back to the original package,
native-verification, preflight, source/control, and workflow bytes. This is a
mechanical release-package boundary, not the full conformance corpus or a
completed release run. POSIX descendants that call `setsid()` remain outside
the current authority, and Windows release runtime authority remains false
until the exact packaged sidecar proves Job Object behavior on native Windows.
