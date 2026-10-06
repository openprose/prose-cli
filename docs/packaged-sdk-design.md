# Packaged SDK operational design

IMP-098, checked October 6, 2026; applies IMP-099. The user selects `~/.prose/cli.toml` and explicit overrides under IMP-097.

## Peer comparison

- [Codex launcher source](https://raw.githubusercontent.com/openai/codex/main/codex-cli/bin/codex.js): package-owned optional platform payload, resolved from installed package.
- [Claude setup](https://code.claude.com/docs/en/setup): native executable through standalone and npm platform payloads; settings remain user-owned.
- [OpenCode installation](https://opencode.ai/docs/): standalone, npm and Homebrew installation.
- [Prime source](https://raw.githubusercontent.com/PrimeIntellect-ai/prime-agent/main/README.md): versioned installer and integrated Python tooling; a different runtime design.

The peers support package-owned platform runtimes and installation-independent preferences, without a universal Python layout. Retain the CLI's existing four native platform payloads. Freeze the existing Python SDK harness into a sibling `prose-agents-sdk` executable using pinned PyInstaller onefile; no user Python, source, provisioning or PATH changes. Archives contain sibling regular executables; npm uses `bin/`; Homebrew installs both to `bin`. Resolve the helper beside the canonical installed CLI executable. Explicit existing test/developer seams remain separate.

## Choice and costs

[PyInstaller usage](https://pyinstaller.org/en/stable/usage.html) documents onefile extraction. [POSIX bundle guidance](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html) documents onedir symlinks and sanitizing dynamic-library variables for child tools. Onefile preserves the existing regular-file archive safety boundary; onedir or bundled interpreter trees would require broader packaging changes. Cost: larger payload/cold start, native build on every supported platform, temporary executable filesystem requirements, and possible leftover extraction directories after forced death. Do not claim forced-death temporary cleanup. Verify frozen-helper supervision including shell descendants.

Build CPython 3.10.20, existing SDK0.22.2/OpenAI3.13.0, and a hash-locked transitive dependency graph with source identities, SBOM/notices and helper hashes in package evidence. Qualified release targets remain macOS/Linux ARM64/x64, with no expansion to unadmitted Windows execution. Verify actual packaged imports, HTTPS certificates, cold discovery, cancellation and tools on native target builders. Preserve existing glibc/platform admission policy.

## Configuration integration

IMP-097 owns canonical discovery, migration, explicit override editing, exact-command explanation and harness-aware defaults. Its initial implementation retains the old harness identifier until IMP-098 switches the built-in harness to `agents-sdk`. SDK contextual defaults are `gpt-6.1-sol` and `openai-api-key`; other selected harnesses receive their own route, never these implicit SDK selections. Explicit incompatible inherited model/auth choices fail before readiness rather than switch account. IMP-098 stacks runtime selection integration on the reviewed IMP-097 contract and retains separate PR ownership.

## Runtime and qualification

The generic Python harness transports opaque instructions/tasks, offers bounded public search/retrieval, local tools and fresh child contexts without interpreting contracts. Shared aggregate bounds cover parent and child model calls, tools, usage and cancellation. Environment filtering is not filesystem isolation; permissions and effective authority must be explicit and tested. Configure API client retries as zero for qualification, so callers own retry allocation.

The workspace assignment allocates $50 total and twelve top-level attempts, one at a time, including children/evaluators/failures. Freeze a matrix and conservative reservation before live calls; unknown terminal accounting retains reservation. No merge or publication is authorized. Release readiness requires independent Rust/Bun/shared/installed-route and live artifact evidence, not process completion alone.
