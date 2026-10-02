# Private local review bundles

This tooling compiles standalone Bun and Rust coordinator sidecars and copies reviewed seed sources into a private local directory. It does not publish, install, edit PATH, add a public Prose command, access provider credentials or execute a model. Current qualification is macOS arm64 only; other platforms fail explicitly rather than inherit a portability claim.

```sh
python3 experiments/weave-seed/distribution/pack.py \
  --bun /absolute/bun \
  --cargo /absolute/cargo \
  --output /absolute/new-review-directory
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s experiments/weave-seed/distribution -p test_pack.py -v
```

The output must not already exist, including as a symlink. Failures retain the newly claimed partial directory for inspection, and a retry requires a new destination. The builder copies sources before building: Bun compiles copied local/run.mjs; Cargo builds copied rust-local with `--offline --locked --release`. Cargo build products live in a separate temporary target directory, never in the copied source or package. Toolchain dependencies must already be available locally. No fetch, installation, upload, signing or registry operation is performed.

The source allowlist consists of named seed subdirectories and source/document/config suffixes. It excludes every target, node_modules, results, receipts, dist, build, hidden directory/file, symlink and unrecognized binary. The copied layout preserves `source/experiments/weave-seed/`, plus the repository MIT license. An exact 21-file supporting allowlist adds the linked readiness, kernel/credential/profile and CLI contribution documentation, plus the deterministic Python reference, its tests and the independent bridge process corpus. It does not recursively copy CLI or harness implementation code. Source-level SDK qualification and the documented Python quick start can therefore run from the copied repository root, `bundle/source`. Missing or symlinked supporting files fail packaging instead of producing broken primary documentation. This allowlist is not an automatic secret detector: review newly added source/config files before sharing any bundle. Existing config examples contain placeholders; no .env file or generated credential/configuration tree is selected.

A manifest identifies inspected source HEAD and dirty status (read-only Git), actual tool versions, platform and SHA256/length of every retained file. It includes dirty/untracked path names, not diff contents. If Git provenance is unavailable it is explicitly null. File inventory binds the actual copied/build outputs; source HEAD does not assert the working tree was clean or an atomic snapshot. The manifest is unsigned and not independent authentication; compare its hash through a trusted channel before consuming it.

The builder relocates a complete copy into a new temporary directory, creates synthetic examples from that copied source, and checks both binaries with an empty environment. It validates --help, check, initial status, first repair and unchanged reuse, actual report bytes, and generated capability paths under the relocated bundle. No external checkout imports are used by these smoke cases. This is installed-artifact mechanics evidence, not semantic/model acceptance.

The binaries do not require an external Bun runtime for their own coordinator commands. Explicit capability executables remain dependencies: the synthetic fixture, Jev/native adapters and configuration helper need Bun, and native acting also needs a separately installed actual Prose CLI plus admitted harness. No kernel image or sentinel image is embedded by this packaging task. Do not claim the bundle supplies a working provider account or self-contained general executor.

Use the [offline installation guide](INSTALL.md) to place a reviewed bundle in a stable private directory. Installation requires its trusted manifest digest and preserves existing installations and subject state. Generate subject configuration after installation so executable paths point to that stable location.

Verify an unchanged bundle without building:

```python
from pathlib import Path
from pack import verify
verify(Path('/absolute/review-directory'))
```

The tooling tests cover allowlist exclusion, existing-destination preservation, manifest integrity failures, relocated source path mapping, the exact supporting-file allowlist, resolved primary documentation links and the copied Python demo. Actual compiled smoke is a separate required gate, recorded in each bundle's smoke.json.

Initial actual build retained at `/private/tmp/imp026-private-bundle-review-1`; manifest SHA256 `19c773c43aa7003002f076dd1f7e1ea1365666ab9e614791ec8d1240d04d87b5`. Both relocated sidecars passed all smoke operations with one cumulative action and fresh reuse. This path is a local retained review artifact, not a download URL, release or committed binary. Later qualification bundles must record their own identities rather than reuse this claim.
