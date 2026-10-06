# Codex compatibility and upgrades

Prose distinguishes a qualified Codex identity from an unqualified version and
from an interface that demonstrably lacks required options. Codex's 0.x alpha
versions do not promise compatibility merely because their numbers increase.
The `exec --json` interface exposes no protocol-version negotiation used by this
adapter. Prose does not substitute app-server or MCP negotiation for it.

The default `qualified` policy retains exact qualified identities. An unknown
identity is not described as an observed runtime protocol failure. On supported
POSIX hosts, Prose checks the installed native `codex exec --help` interface with
a credential-free, five-second probe and a 32 KiB limit per output stream. It
requires actual option declarations for JSONL, stdin/cwd/model selection,
ephemeral sessions, user-config/rule isolation, and sandbox/configuration
selection. Mentions in descriptive text and longer option-name prefixes do not
count. A failed probe leaves compatibility unknown; absent required declarations
make the advertised interface incompatible. Neither condition launches the task.

To explicitly attempt an unqualified version without downgrading your everyday
Codex installation:

```sh
prose --harness codex --codex-compatibility probe cli doctor --json
prose --harness codex --codex-compatibility probe --permission-mode read-only contract.md
```

Choose the permissions appropriate to your actual task. The example selects
read-only explicitly; selecting `probe` never changes permissions, credentials,
model, harness, transport, billing ownership or instruction placement. Prose
continues to enforce the existing ordered exec-json events and required terminal
record. Missing, malformed or unsupported runtime events still fail. A successful
run reports its transport result; it does not establish general Codex
compatibility or prove that a contract's requirements were satisfied.

The setting can also be supplied as `codex_compatibility = "probe"` in the
existing CLI configuration or `PROSE_CODEX_COMPATIBILITY=probe`. The usual
project/user/environment/flag precedence applies, and `cli config explain`
reports its actual source. Only `qualified` and `probe` are accepted. An active
probe policy with another harness is a configuration error rather than an
implicit harness switch. Keep experiments scoped to a project or invocation;
a future qualification update can restore ordinary qualified admission.

Machine errors retain `HARNESS_INCOMPATIBLE` as the admission-error category,
with an explicit `details.compatibilityStatus`: `unqualified`, `incompatible`,
or `probe-failed`. Their messages and actions distinguish those states. Unknown
versions receive an explicit probe recovery and a Prose-update alternative,
rather than a global Codex downgrade. Codex run and doctor reports include
`codexCompatibility.qualification` (`qualified`, `unqualified`, or `unknown`).
Doctor also reports the selected policy. Doctor readiness under `probe` means
mechanical prerequisites passed; the version remains visibly unqualified.

Exact previously qualified identities retain their existing admission on
Windows. The expanded capability-probe path is unavailable on Windows because
the current process-host command probe has not been qualified there. This change
does not broaden Windows runtime or process-containment claims.

## Validation and limitations

Shared black-box cases run against both compiled implementations: exact qualified
admission; default unqualified rejection; explicit unqualified execution; missing
JSON/sandbox flags; longer-token and descriptive-text lookalikes; wrong-stream,
nonzero and oversized native help; and unknown runtime events/missing terminal
records; and failed-probe doctor/inventory reporting. Product unit tests additionally check declaration parsing and policy
configuration. These tests use controlled harnesses, not paid model calls. The release-package
corpus advances to v2 to bind the updated help bytes; the frozen v1 corpus and
historical reports remain intact.

The [native observation](codex-compatibility-qualification.json) records installed
Codex `0.159.0-alpha.12.1`, exact executable/help/contract digests and required
advertised options. It includes an actual provider-free native help observation and both compiled
products' doctor interaction with that same native executable. With fresh empty
credential roots, both products pass compatibility admission and reach
`HARNESS_NEEDS_AUTH`, retaining the unqualified label. The observed binaries use
test seams and a sentinel transport image; they are not distributable artifacts.
It does not qualify exec-json execution, tools, permission enforcement,
developer/base instruction configuration, model identity, cancellation or
contract fulfillment. No new exact identity or version family is admitted by
this change. Model-backed qualification requires a separately approved bounded
allocation and retained native execution evidence before changing the qualified
identity inventory.

Related documentation:

- **Problem — [issue 39](https://github.com/openprose/prose-cli/issues/39):** the
  published RC's admission rejection after a routine Codex upgrade.
- **Upstream — [OpenAI non-interactive documentation](https://developers.openai.com/codex/noninteractive):**
  structured exec-json output; it does not provide a compatibility guarantee for
  all alpha releases.
- **Implementation — [shared compatibility contract](../cli/shared/capabilities/adapters/codex-compatibility.v1.json):**
  required native option declarations and probe limits.
- **Release — [current release guide](cli-release-next.md):** source and published
  artifact identities remain distinct; this change publishes nothing.
