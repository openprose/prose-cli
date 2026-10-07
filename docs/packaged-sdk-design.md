# Packaged SDK operational design

IMP-098, checked October 6, 2026; applies IMP-099. The user selects `~/.prose/cli.toml` and explicit overrides under IMP-097.

## Peer comparison

- [Codex launcher source](https://raw.githubusercontent.com/openai/codex/main/codex-cli/bin/codex.js): package-owned optional platform payload, resolved from installed package.
- [Claude setup](https://code.claude.com/docs/en/setup): native executable through standalone and npm platform payloads; settings remain user-owned.
- [OpenCode installation](https://opencode.ai/docs/): standalone, npm and Homebrew installation.
- [Prime source](https://raw.githubusercontent.com/PrimeIntellect-ai/prime-agent/main/README.md): versioned installer and integrated Python tooling; a different runtime design.

The peers support package-owned platform runtimes and installation-independent preferences, without a universal Python layout. Retain the CLI's existing four native platform payloads. Freeze the existing Python SDK harness with pinned PyInstaller. Both Mac targets use standard onedir: the regular sibling `prose-agents-sdk` executable and its complete `prose-agents-sdk-runtime/` support directory. Linux retains onefile and its existing native dependency closure. No user Python, provisioning or PATH helper is required. npm uses `bin/`; Homebrew installs the helper and Mac support directory together under `bin`. Resolve the helper beside the canonical installed CLI executable. Explicit existing test/developer seams remain separate.

## Choice and costs

[Pinned PyInstaller usage](https://pyinstaller.org/en/v6.22.3/usage.html) and [POSIX bundle guidance](https://pyinstaller.org/en/v6.22.3/common-issues-and-pitfalls.html) describe directory bundles, declared aliases and onefile extraction before Python entry. A retained Mac Intel npx installation timed out in the unchanged five-second version probe despite the fast Python version entry. That observation does not identify which startup phase consumed the time. Mac onedir removes unconditional extraction while preserving execution of the real frozen helper; fresh native evidence must demonstrate cold discovery. Linux onefile retains its temporary executable filesystem requirement and possible leftover extraction directories after forced death. Do not claim forced-death temporary cleanup. Verify frozen-helper supervision including shell descendants.

Mac packaging binds every physical support file, directory and declared relative alias in the SDK receipt. Validate the whole confined graph before copying or extraction, write regular files before aliases, and recheck complete bytes, modes and targets after relocation and helper tests. Verify every actual Mach-O target and signature after final signing. COLLECT declarations establish producer membership and alias evidence; the final inventory binds signed bytes. The twelve distribution identity fields remain unchanged because the receipt digest binds this complete inventory.

[npm's pinned pacote extractor](https://github.com/npm/pacote/blob/v21.5.1/lib/fetcher.js#L427-L430) drops archive links. Positive npm fixtures therefore use a complete linkless payload; missing required aliases remain an admission failure. Each actual Mac build must pass complete inventory checks after npm installation before that target is qualified. The linkless ARM prototype does not establish Intel compatibility.

The SDK support inventory has an explicit 8,192-entry envelope. Generic archives retain their 128-member, no-link policy. The SDK allowance applies only to an authenticated SDK context with exact declared membership, and remains subject to 256 MiB per file, 512 MiB total support and whole publication payload, 2 MiB encoded receipt and 768 MiB expanded installed payload limits. These are simultaneous bounds; they do not establish that a candidate fits. The prior Mac onefile archive already contained 2,532 support declarations, making the generic member count unsuitable for the directory bundle.

Build CPython 3.10.20, existing SDK0.22.2/OpenAI3.13.0, and a hash-locked transitive dependency graph with source identities, SBOM/notices and helper hashes in package evidence. Qualified release targets remain macOS/Linux ARM64/x64, with no expansion to unadmitted Windows execution. Verify actual packaged imports, HTTPS certificates, cold discovery, cancellation and tools on native target builders. Preserve existing glibc/platform admission policy.

## Configuration integration

IMP-097 owns canonical discovery, migration, explicit override editing, exact-command explanation and harness-aware defaults. Its initial implementation retains the old harness identifier until IMP-098 switches the built-in harness to `agents-sdk`. SDK contextual defaults are `gpt-6.1-sol` and `openai-api-key`; other selected harnesses receive their own route, never these implicit SDK selections. Explicit incompatible inherited model/auth choices fail before readiness rather than switch account. IMP-098 stacks runtime selection integration on the reviewed IMP-097 contract and retains separate PR ownership.

## Runtime and qualification

The generic Python harness transports opaque instructions/tasks, offers bounded public search/retrieval, local tools and fresh child contexts without interpreting contracts. Shared aggregate bounds cover parent and child model calls, tools, usage and cancellation. Environment filtering is not filesystem isolation; permissions and effective authority must be explicit and tested. Configure API client retries as zero for qualification, so callers own retry allocation.

The workspace assignment allocates $50 total and twelve top-level attempts, one at a time, including children/evaluators/failures. Freeze a matrix and conservative reservation before live calls; unknown terminal accounting retains reservation. No merge or publication is authorized. Release readiness requires independent Rust/Bun/shared/installed-route and live artifact evidence, not process completion alone.

## Frozen production interface (October 6, 2026)

The production built-in is `agents-sdk`, model `gpt-6.1-sol`, authentication
`openai-api-key`, JSONL transport and `user-provider` billing. Preserve explicit
alternative harness bundles under IMP-097 precedence. SDK permissions are host
OS permissions, with no filesystem or network sandbox; no selected permission
flag may imply enforcement the helper does not implement.

Resolve `prose-agents-sdk` beside the canonical CLI executable, following the
CLI's symlink to the installed payload before selecting its sibling. The helper
must be a regular executable; reject a sibling helper symlink. Relocation keeps
the CLI, helper and complete Mac support directory together. Never search PATH as a
production fallback or accidentally pair a CLI with another installation's
helper. Existing explicit developer/test seams remain separate from production.
Repair a missing/incompatible sibling by reinstalling the CLI through its current
installation route. Missing or blank `OPENAI_API_KEY` fails with
`HARNESS_NEEDS_AUTH`, actionable setup guidance, no inference and no credential,
model or harness fallback. Explanation remains pure even without either key or
helper; it resolves static configuration and never probes executable/credentials.

Every production SDK report emits all eleven `nativeLimits` fields frozen in
`cli/shared/fixtures/adapters/sdk-production.json`. `maxAggregateRequests` and
`maxAggregateHostedWebCalls` equal resolved `maxTurns`; fixed defaults are 80
function tools, 500000 observed total tokens, 256000 serialized request input
bytes, eight children and one child depth. The observed token limit stops the
next request, not an in-flight response. Historical four-field records remain
valid; partial mixtures of the old and expanded limits are invalid.

Optional `usageObservation` and `modelIdentity` appear at the normalized result
root and under `runner-error.details` on failure. `modelIdentity.serviceTier`
records requested `default` and an allowlisted observed tier array. Never promote
these observations into authoritative `usage`: SDK usage stays
`{status:"unavailable"}` because incomplete/cancelled provider work and currency
remain unknown. Preserve observations already received on failure or cancellation without
inventing zeros for absent token counters. If stdio has already closed before an
observation arrives, retain unknown usage rather than infer provider completion.
The helper includes current usage on existing start/tool_call/tool_result events;
this does not add event types or make total run usage known. Provider IDs, exception bodies and raw unknown
fields are never retained.

The ten usage-observation fields and six flattened token counters are closed by
`sdk-observation.schema.json`. Counts are nonnegative safe integers;
`outstandingProviderRequestCount` is always null and `totalRunUsageKnown` always
false. Retain a usage group only when its mandatory counters/constants are valid;
drop unknown properties and unknown/invalid token-counter entries. No invalid
mandatory counter may be replaced with a zero. Requested model identity comes
from the resolved invocation, rather than trusting an arbitrary event's request
string. Observed models are unique sorted ASCII public identifiers, at most 128
characters and 128 identities; unsupported values are dropped. Omit the whole model group when `observed` is not an array, `serviceTier` is absent
or not an object, its `observed` is not an array, or its `requested` is not
`default`. Valid empty observed arrays retain honest unknown identity/tier state.
Within a structurally valid group, observed tiers are unique sorted members of
the frozen enum; unsupported entries are dropped.
Absent groups stay absent. Result/error observations must be bounded and must
never forward raw usage, provider request IDs or unrecognized values.

For `harness use`, parse file, environment and flag layers before validating the
explicit selection target, then perform contextual model/auth semantic checks.
Malformed configuration files retain `CONFIG_INVALID` precedence. Missing required
explicit Prime/OMP selection options yield `INVOCATION_INVALID` before an ambient
old route can fail against the SDK default. This ordering does not suppress layers
or skip parsing their contents.

Recipe platform support covers the existing four native release payloads. Archive
IDs remain `linux-arm64` and `linux-x64`; runtime admission uses
`linux-arm64-gnu` and `linux-x64-gnu`, requiring GLIBC 2.34 or newer. Musl and bare
unspecified Linux platform identities are not admitted. This
frozen contract is acceptance intent; it does not itself establish native build,
installed-route, live fulfillment or release qualification. The shared black-box
cases and independent product controls must prove each behavior before release.

Native `SetupError` reports carry only a closed `setup_reason`: credential-or-permission,
model-unavailable or local-input. Outer failures retain `HARNESS_FAILED` and exit 22,
with `nativeFailure.kind=setup` and `setupReason` only for a recognized reason.
The runner owns fixed recovery actions from the shared SDK oracle, for both human
and JSON output. Unknown/missing reasons stay execution failures. Never forward
arbitrary native messages or infer request completion, usage or billing from a
setup classification; preserve valid observations already received.


### Homebrew preservation of packaged payloads

The native Homebrew rehearsal uses the public Formula `preserve_rpath` convention to retain the SDK libraries' relative dylib identities and `skip_clean` scoped to `bin/prose`, `bin/prose-agents-sdk` and `bin/prose-agents-sdk-runtime`. Homebrew continues its other linkage checks. The complete signed SDK tree must retain its declared bytes, modes and directories through installation; neither a cleanup exclusion nor relative-ID preservation alone establishes that custody. Real install, genuine upgrade, collision refusal and cold helper probes remain qualification gates. The formula keeps the executable and its SDK helper/support tree as canonical keg siblings.

The Linux freezer verifier reads the final `binaries` field in the pinned PyInstaller Analysis TOC. Its input binary list is a distinct record and cannot establish final selection. Exactly one final libgcc destination must bind to the pinned supplier path and bytes; complete TOC provenance and downstream archive, ELF, origin and symbol-closure checks remain required.


Wheel origin inspection selects the direct owning distribution's `.dist-info/RECORD`, binding its normalized metadata name and exact version to the lock. `Distribution.files` also lists vendored metadata resources; those remain part of the complete inventory but cannot substitute for the owning RECORD. The original RECORD bytes and native/license file hashes and sizes remain required. Selection anchors to the public distribution base, permitting consistent base aliases while refusing aliases of the owning metadata directory and foreign locators.

The development Homebrew rehearsal recognizes the existing explicit SDK-free fixture marker only in non-publishable development context. It retains the original manifest and uses the classified SDK identity consistently for archive and installation checks. An SDK-free context cannot contain packaged helper/support/receipt/notices members or SDK evidence claims. Production qualification continues to require the complete packaged SDK identity and custody.
