# Published kernel startup

Published startup is implemented in both Bun and Rust. The provider-free
startup checks described below establish acquisition and transport behavior;
they do not establish model conformance or contract fulfillment. See the
[release guide](../cli/release/README.md) for current distribution qualification.

Ordinary compiled builds resolve `https://pkg.prose.md/kernel.md` before launching an installed harness. They accept only a redirect into the official origin's immutable `/releases/<release>/core/README.md` layout, fetch that release's descriptor and inventory, verify the inventory digest and kernel entry digest, and freeze the verified bytes for that invocation. They do not parse Markdown, install packages, create locks or implement Prose commands.

The trust anchor is the official HTTPS origin; a checksum retrieved from that origin is integrity evidence, not independent authentication. Startup has a separate 15-second total retrieval budget, a 256 KiB metadata limit and a 32 KiB kernel limit. No automatic retry, stale-cache fallback, echo fallback or provider substitution occurs. Rust cancellation is checked between requests; an in-flight blocking request remains bounded by the retrieval deadline. The selected kernel then uses the existing native execution timeout and capture limits.

The verified full kernel is delivered as appended instructions. The canonical task JSON is separate:

| Harness | Instruction mechanism | Credential routes covered by preparation |
|---|---|---|
| Codex | Additional `developer_instructions`; native defaults retained | Existing ChatGPT login, OpenAI API |
| Claude | `--append-system-prompt-file` | Anthropic API; existing subscription route remains supported |
| Prime | `--append-system-prompt` text | Anthropic, OpenAI, OpenRouter |
| OMP | `--append-system-prompt` file | Anthropic, OpenAI, OpenRouter |
| Agents SDK | Instructions file appended to generic harness instructions | OpenAI API |

These channels have native names and are not all a literal wire-level system role. Codex serializes its instruction content as developer messages. None of these startup paths inserts the kernel into the user task. Model identifiers remain user-selected and opaque; successful readiness does not prove model availability, API authentication or identical model behavior. Existing version admission, credential separation, native permissions and platform restrictions remain in force.

## Use

Build either implementation normally, install a supported native harness, and select it explicitly or through the existing saved harness command:

```sh
prose cli harness use codex
prose --model MODEL --permission-mode workspace-write run hello.prose.md
```

Use a model available to your account. Existing ChatGPT login remains Codex's default route. API keys belong in the parent environment with an explicit `--auth-profile`; never in argv or committed files. Prime and OMP require a qualified provider/model and explicit profile. See [credential routes](api-credentials.md).

Ordinary published-kernel builds default to native completion. Artifact fulfillment must still be checked independently. Help, version and local configuration/doctor operations do not retrieve the kernel. Doctor's optional `imageSource: published-on-run` identifies deferred selection; its `image` object describes only the embedded diagnostic fixture, not an executed fallback. Run `--dry-run --output json` to resolve the kernel and check the selected native setup without starting a model.

Default harness selection is unchanged: an unconfigured hosted selection still reports unavailable. No silent harness selection is introduced.

## Fixed inputs and tests

| Build selection | Instructions used for an installed-harness run | Default output contract | Test seams |
| --- | --- | --- | --- |
| Ordinary development or release build | Verified published kernel acquired on run | `native` | Disabled |
| Explicit image directory, bundle and checksum | Verified embedded image | `image-envelope` | Disabled |
| Bun `build:test` or Rust `prose-cli/test-seams` | Embedded `sentinel-v1` transport fixture | `image-envelope` | Enabled; development only |

Ordinary builds also embed `echo-v0` for diagnostics. Doctor describes that
fixture while `imageSource: published-on-run` identifies deferred startup;
neither means a failed kernel retrieval will execute the fixture.

Explicit image builds continue to consume a verified image directory, bundle and checksum; they do not contact the moving kernel entry. They preserve the image-envelope default unless the caller selects `--output-contract native`. Both ordinary compiled implementations append Codex instructions for explicit images too. Bun's earlier `--codex-instructions base|framed` remains an explicit-image experiment option; published startup rejects those modes.

Source-level test seams and dedicated test builds retain hermetic fixture behavior. They do not establish the ordinary compiled default; the shared compiled-process test separately checks Codex append delivery, task separation, model selection, child failure and timeout. The image eligibility check validates embedded image structure; it does not freeze a moving published kernel or establish language qualification. Current release policy and external authorities are recorded in the release guide.

Runtime project-kernel lock selection is not implemented here. A fixed image build is the current deterministic pinning route. Initialization and package-lock semantics belong to the separately planned command interface. No company lock or installed dependency is changed by startup.

## Supplying tool configuration

Installed adapters construct the child environment from a shared base allowlist
and the explicitly selected credential route. Arbitrary application variables
such as `MY_TOOL_PATH` are not forwarded. The CLI does not turn a custom
environment variable into an agent tool setting. Supply an ordinary configuration
file accessible from the selected working directory, and have the contract require
the agent to read it. A tool executable path, its arguments and its expected
digest can be explicit fields in that file; the executing agent remains
responsible for honoring those requirements.

This provider-free POSIX example creates a deterministic tool and configuration
in a fresh directory, then independently checks the configured digest and output:

```sh
tool_demo_root=$(mktemp -d)
cd "$tool_demo_root"
cat > tool.sh <<'EOF'
#!/bin/sh
printf '%s\n' 'tool-ready'
EOF
chmod 700 tool.sh
bun - <<'JS'
const executable = `${process.cwd()}/tool.sh`;
const hash = new Bun.CryptoHasher("sha256");
hash.update(await Bun.file(executable).arrayBuffer());
await Bun.write("tooling.json", JSON.stringify({
  executable, sha256: hash.digest("hex"), args: [],
}, null, 2));
JS
bun - <<'JS'
const config = await Bun.file("tooling.json").json();
const hash = new Bun.CryptoHasher("sha256");
hash.update(await Bun.file(config.executable).arrayBuffer());
if (hash.digest("hex") !== config.sha256) throw Error("Tool digest mismatch");
const child = Bun.spawn([config.executable, ...config.args], {
  env: {PATH: "/usr/bin:/bin"}, stdin: "ignore", stdout: "pipe", stderr: "inherit",
});
const output = await new Response(child.stdout).text();
if (await child.exited !== 0 || output !== "tool-ready\n") {
  throw Error("Unexpected tool result");
}
console.log(output.trim());
JS
cat > check-tool.prose.md <<'EOF'
Read tooling.json from the supplied working directory. Verify the executable's
SHA-256 before invoking it with the supplied arguments. Report its observed
output. Do not infer tool configuration from ambient custom environment variables.
EOF
```

The last two Bun commands verify the fixture directly, without an agent or
model call. They do not prove the contract was executed. With a separately
authorized native run, pass `--cwd "$tool_demo_root"` and
`run check-tool.prose.md` to the exact chosen CLI, together with explicit harness,
model, authentication and native permissions. Use a fresh directory for each
attempt; retain observed tool effects and assess fulfillment independently.
This file supplies no permission grant, environment-forwarding extension or
filesystem isolation. Native permissions and filesystem access still have to
allow the configured path. Do not put credentials in the configuration.

## Verification and limitations

The two implementations use the same acquisition policy, image template and shared HTTP fixtures. Tests cover exact bytes and digests, origin/path rejection, malformed or modified metadata/content, size bounds, task separation and native argv limits. The selected release and content identity appear in ordinary image/result metadata. The public endpoint was also checked through dry runs of both compiled implementations, without a model.

A 20-route local readiness matrix covers both runners and the ten credential routes above (two Codex, one Claude, one SDK, three Prime, three OMP). Synthetic API keys test selection only; `ready` must not be read as authenticated. Existing platform/version restrictions remain; these checks were on macOS ARM64. Stronger ambient context control belongs to IMP-009. Live provider/model calls remain deferred until credentials and bounded attempt allocations are supplied.

**Task — [IMP-008](https://github.com/openprose/openprose-workspace/blob/main/work/items/IMP-008.md):** authorization, branches and evidence.
