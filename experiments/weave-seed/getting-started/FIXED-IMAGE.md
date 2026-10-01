# Stage a private fixed kernel image

The native executor requires a fixed image containing exactly one model-visible payload at `payload/kernel.md`. Its aggregate image identity must match the selected kernel bytes and the configured `expectedImageSha256`. An ordinary CLI that resolves a moving published kernel at run time is not that fixed-image profile.

Use the existing CLI's image format and build tools. This helper only fills in the private image manifest, preserves your exact selected kernel, and invokes the selected checkout's official `image_bundle.py build` and `check` commands. It does not interpret Markdown, resolve adopted definitions, download a kernel, build a native CLI, install dependencies, call a provider, or publish anything.

## Stage the image

You need Python 3, an explicitly trusted full CLI checkout, an existing exact kernel file, its caller-asserted full 40-character lowercase source revision, and a fresh absolute output directory whose parent exists. A private weave bundle does not contain the full CLI image/build tooling; supply that checkout separately.

From the weave source repository root, or `bundle/source` in a private review bundle:

```sh
python3 -B experiments/weave-seed/getting-started/stage-kernel-image.py \
  --cli-source /absolute/trusted/prose-cli \
  --kernel /absolute/subject/kernel/README.md \
  --source-revision FULL_40_CHARACTER_LOWERCASE_REVISION \
  --output /absolute/new-private-image
```

The selected CLI checkout is trusted executable input: the helper runs its image-bundle Python script locally. This is not a sandbox for an untrusted checkout. The tool requires absolute paths, regular bounded files, valid UTF-8, no BOM/NUL/CR bytes, and a nonblank kernel. It refuses to convert line endings or change kernel bytes. The current profile caps the kernel at 32 KiB, each template/contract/tool file at 1 MiB, and the resulting bundle at 4 MiB. Kernel symlinks are rejected on the tested POSIX route.

The helper copies the current `cli/shared/image/kernel-startup` manifest template and its three declared contract files into a new image directory, after checking their declared digests. It accepts the template's existing one-payload kernel layout, sets `imageVersion` to `private-review`, records the supplied revision in `semanticSourceRevision`, and sets `releaseEligible` to false. Other language/runtime metadata remains the selected template's metadata; this is not a semantic compatibility certification. The helper does not use a sentinel image or a release-eligibility override.

Existing output directories and symlinks are refused. Created directories are private and staged files have owner-only permissions. If official staging/checking fails, a newly created partial output can remain for inspection; it is not an accepted image. Inspect it and select a fresh destination for a retry. Do not reuse or overwrite a previously reviewed image silently.

Successful stdout is one JSON record, also saved as `staging.json`. It identifies the image directory, bundle/checksum paths, exact kernel length/digest, template/tool/contract digests, bundle digest, expected aggregate image digest, and unexecuted Bun/Rust build argument arrays. The source revision is explicitly **caller-supplied provenance**: the helper does not inspect Git, verify that revision contains the kernel, or authenticate its publisher. Exact byte hashes establish identity, not source authority or fulfillment.

The aggregate is the existing format's SHA-256 over these exact bytes:

```text
UTF8("payload/kernel.md") + NUL + ASCII(decimal kernel byte length) + NUL
+ exact kernel bytes + NUL
```

`expectedImageSha256` is that aggregate digest. `kernelSha256` hashes the raw kernel alone; `bundleSha256` hashes the serialized bundle. These three identities have different meanings and must not be substituted for each other. The official tool independently checks the staged manifest, payload and contract bytes.

## Build explicitly, then configure the executor

Review `staging.json` and use its `buildCommands` with installed, identified tools. The helper does not execute them. Each record supplies a working directory and literal argument array; the Rust record also supplies the three image-selection environment variables and a private Cargo target directory. Replace the leading `bun` or `cargo` name with your explicitly selected executable if it is not on your trusted PATH.

The Bun build uses the full checkout's `cli/bun/scripts/image-bundle.ts build` with all three explicit image paths and a private output executable. The Rust build uses `cargo build --offline --locked` with `OPENPROSE_IMAGE_SOURCE_DIR`, `OPENPROSE_IMAGE_BUNDLE` and `OPENPROSE_IMAGE_BUNDLE_CHECKSUM`. Offline Rust builds require dependencies already cached. Bun/native builds also require the selected checkout's normal build dependencies. No dependency installation is performed by staging.

Do not add `--require-release-eligible`: this candidate is intentionally private and not release eligible. Building it is separate from approving its kernel, granting effect permissions or authorizing provider spend. Consult the selected CLI revision's build documentation when its build interface differs; there is no silent fallback to a published or sentinel image.

After an explicitly performed build, supply the resulting CLI path and its actual executable SHA-256 to the reviewed BYOK setup manifest, along with this helper's `expectedImageSha256`. Keep the original kernel file selected as the observer's kernel. Use the existing provider-free native readiness route to inspect the image delivery profile; readiness does not establish provider authentication or semantic correctness. The executor rechecks both the fixed image aggregate and delivered kernel bytes before accepting transport completion.

Kernel changes require a newly reviewed stage/build and updated executor/configuration binding. Do not replace image bytes in place under an existing accepted identity or reset unresolved checkpoint state to adopt a new build.

## Offline checks and limits

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s experiments/weave-seed/getting-started -p test_stage_kernel_image.py -v
```

Four tests use self-contained trusted-template and synthetic bundle-tool fixtures, so they also run from a copied private bundle without a hidden full-checkout dependency. They check exact payload preservation and aggregate calculation, build/check invocation, private output, recorded tool identity, release-ineligible rejection, malformed revision, blank/invalid/oversized kernel, template contract tampering, and refusal of existing destinations. These synthetic tool tests do not qualify the official image format or an installed CLI. Actual staging with a selected full CLI checkout and separate native build/readiness evidence is a distinct qualification step.
