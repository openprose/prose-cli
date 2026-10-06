# Image bundle tool

`image_bundle.py` is the stdlib-only staging authority between a
language-owned image-format-v1 directory and the Rust/Bun runner products. It
does not interpret or author OpenProse language text. See `FORMAT.md` for the
binary representation.

Build and re-check a staged image:

```sh
python3 image_bundle.py build ../echo-v0 ../embedded/current.bundle.bin \
  --checksum ../embedded/current.bundle.sha256 --require-release-eligible
python3 image_bundle.py check ../echo-v0 ../embedded/current.bundle.bin \
  --checksum ../embedded/current.bundle.sha256 --require-release-eligible
```

The committed `echo-v0` image passes that structural gate for a functional
alpha, but deliberately has `semanticStatus: not-applicable` and does not
execute OpenProse. The retained `sentinel-v1` image still intentionally fails
the gate. Passing it is necessary but not sufficient for a semantic or
general-availability release: the language-owned replacement image and its
release-profile digest authority remain external to this transport-only tool.

For a data-only replacement, generate a bundle/checksum from the replacement
image, then point a product build at those three paths:

```sh
OPENPROSE_IMAGE_SOURCE_DIR=/absolute/image \
OPENPROSE_IMAGE_BUNDLE=/absolute/current.bundle.bin \
OPENPROSE_IMAGE_BUNDLE_CHECKSUM=/absolute/current.bundle.sha256 \
cargo build --manifest-path ../../../rust/Cargo.toml -p prose-cli

bun ../../../bun/scripts/image-bundle.ts build \
  --image-dir /absolute/image \
  --bundle /absolute/current.bundle.bin \
  --checksum /absolute/current.bundle.sha256 \
  --outfile /absolute/prose
```

Neither path requires an adapter or runner source edit. The build first checks
the source directory, exact staged bytes, and checksum; any drift fails closed.
Supply all three paths. This explicit selection disables published-kernel
acquisition and defaults to the image's terminal-envelope mode; select
`--output-contract native` explicitly when appropriate for the chosen image.
It pins the embedded instruction bytes, not the harness, model, toolchain,
dependencies or every other campaign input.

To inspect the existing committed diagnostic bundle and build an offline,
fixed-image Bun executable from the repository root:

```sh
bun cli/bun/scripts/image-bundle.ts check \
  --image-dir cli/shared/image/echo-v0 \
  --bundle cli/shared/image/embedded/current.bundle.bin \
  --checksum cli/shared/image/embedded/current.bundle.sha256
bun cli/bun/scripts/image-bundle.ts build \
  --image-dir cli/shared/image/echo-v0 \
  --bundle cli/shared/image/embedded/current.bundle.bin \
  --checksum cli/shared/image/embedded/current.bundle.sha256 \
  --outfile cli/bun/dist/prose-echo
```

This selects the nonsemantic echo fixture, not OpenProse language execution.
The existing image staging/validation implementation still uses Python
internally; these commands do not introduce another implementation or dependency.

The Rust runner's explicit provider-free test build is separate from the
data-only replacement path:

```sh
cargo build --manifest-path ../../../rust/Cargo.toml -p prose-cli \
  --features prose-cli/test-seams
```

That development-only feature embeds `sentinel-v1` and enables the mock and
conformance seams. Ordinary development and release builds without image
overrides acquire the verified published kernel for installed-harness runs;
they retain `echo-v0` only as a diagnostic fixture and keep test seams disabled.
See [published startup](../../../../docs/kernel-startup.md). Combining `--release` with
the test-seams feature fails closed.
