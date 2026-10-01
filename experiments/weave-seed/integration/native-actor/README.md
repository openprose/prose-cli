<a id="native-cli-actor-adapter"></a>

# Native CLI executor adapter

This local process adapter takes an existing `openprose.weave-input/1` envelope on stdin and invokes an explicitly selected installed Prose CLI through its supported native route. It does not implement another agent loop, interpret Markdown, select a provider automatically, track billing, repair fixtures, or establish fulfillment. It has no lab/runtime-file dependency.

```sh
bun --no-env-file experiments/weave-seed/integration/native-actor/run.mjs --config /absolute/subject/actor.json
bun --no-env-file experiments/weave-seed/integration/native-actor/actor.test.mjs
```

Exported `invokeNative(configPath, envelope, {signal})` offers the same action as a library. Errors from the standalone wrapper are intentionally generic; native stdout/stderr and credentials are not forwarded. Success returns only native-completed, attempt, delivered kernel hash, and the requirement for fresh observation/assessment. Keep the outer pending-action protocol: any error may follow an effect and must never trigger blind replay.

## Explicit setup

Copy config.example.json into the subject root as actor.json and replace every placeholder. `executable` is an absolute installed CLI path and executableSha256 binds its actual bytes. `cwd` resolves relative to actor.json. Kernel and task are relative within cwd; task is the ordinary program path. The adapter supplies `run TASK` unchanged, not an invented command mapping. Executor config must remain inside the selected subject root, although the executable can be elsewhere.

This first version supports `agents-sdk` and `openai-api-key` only. That is an explicit capability scope: current CLI native budget flags are supported only by Agents SDK. Install the optional generic prose-agents-sdk harness as documented in the [separate full CLI checkout guide](https://github.com/openprose/prose-cli/blob/main/docs/agents-sdk-adapter.md) and provide your own API credential through the parent environment. The adapter forwards only named environmentKeys; the CLI applies its own narrower child environment rules. No account login or credential-file scraping is implemented. Harness availability/admitted versions and actual model access remain user setup requirements.

Use the real CLI's provider-free dry run to inspect readiness and the image identity with the same explicit model/harness/auth/cwd and limits. Example (substitute your values):

```sh
/absolute/prose --harness agents-sdk --auth-profile openai-api-key --model YOUR_MODEL \
  --native-max-turns 8 --native-timeout 120000ms --native-tool-timeout 15000ms \
  --timeout 150000ms --cwd /absolute/subject --output-contract native \
  --dry-run --output json run program.md
```

Record the reported languageImage.sha256 as expectedImageSha256, after reviewing that selection. Retain the corresponding exact kernel bytes as kernel/README.md and select them as the binding's kernel. Readiness is not successful provider authentication or a spend authorization.

This adapter now requires a **fixed-image CLI build**, documented in cli/shared/image/bundle/README.md. Before dry-run it invokes the bounded local `cli doctor --json` operation. Admission requires schema `openprose.doctor-report/1`, absence of the imageSource property, build.testSeamsEnabled exactly false, and image.sha256 equal to expectedImageSha256. A published-on-run, unknown, malformed, mismatched or test-seam report fails in phase image-policy before readiness or action. Doctor metadata is trusted only as output from the explicitly digest-pinned executable; it is not independent attestation.

There is no runtime kernel-file override in the inspected CLI. Ordinary published-on-run builds are **not admitted**, even when a dry-run happens to show matching content: they may acquire a different kernel between checks and execution. Building a fixed-image binary remains a public-UX qualification gap; this adapter does not silently add an override, download/build a binary or install packages. Use the fixed build's doctor report to establish its configured identity, then dry-run to inspect the selected harness route. Post-run delivered-kernel checks remain a second gate rather than the sole protection.

## Coordinator binding

In the outer integration/local configuration use:

```json
{
  "actor": ["/absolute/bun", "--no-env-file", "/absolute/repository/experiments/weave-seed/integration/native-actor/run.mjs", "--config", "/absolute/subject/actor.json"],
  "environmentKeys": ["PATH", "OPENAI_API_KEY"],
  "timeoutMs": 270000
}
```

Merge those fields into the full existing runConfig schema; this fragment is not a complete configuration. Select actor.json as **evidence**, program.md as **contract**, and the exact local kernel as **kernel**, alongside every other governing/evidence dependency. Any separate evaluator config/question belongs in selected evidence too. Outer process timeout must exceed 2 × readinessTimeoutMs + processTimeoutMs plus input/setup margin; the example uses 270 seconds for 45+45+165 second defaults. Budgets are cumulative in the outer checkpoint. This adapter's maxTurns/token limits are not an aggregate dollar budget.

The adapter verifies exact envelope shape, non-gap freshness, payload identity, strict JSON, every observed file digest and actual current bytes, and the config/kernel/task membership and roles. It rechecks after readiness, immediately before launch. It pins the executable digest on both validations. All source files must be regular bounded files inside the trusted subject root; aggregate source bytes are capped at 256 KiB. This is sequential revalidation, not an atomic world snapshot or defense against hostile concurrent filesystem replacement. Do not mutate config, dependencies or capabilities during execution. Transitive native-harness/dependency identity is not fully pinned by the CLI executable digest; bind changes through capabilityVersion and admission controls.

## Completion, bounds and limitations

The exact direct argv contains harness, auth profile, model, native budgets, outer timeout, cwd and native output contract. The preceding doctor gate checks fixed-image admission. Dry-run checks ready/wouldStartModel, admitted adapter/model, strict system-append placement, image identity, limits and user-provider billing category. It does not claim to independently authenticate billing account identity. Actual execution requests JSONL; exactly one successful runner.completed plus matching image, limits and delivered kernel is required. Completion remains transport evidence only.

Input is limited to 1 MiB and five seconds, UTF-8 is strict, config and source files are bounded, and JSON duplicate keys/nonfinite numbers/unpaired surrogates are rejected. Each CLI process has an explicit deadline and combined stdout/stderr capture budget. Overflow/deadline/cancellation closes capture and fails without printing private child output. POSIX cancellation attempts SIGKILL on the spawned process group; Windows targets the direct child. Separately detached descendants/native-owned sessions can survive. The outer synchronous process bridge can itself kill the adapter before its handlers run; it supplies no whole-tree sandbox. Hard kills and escaped effects remain uncertain, requiring host reconciliation. No paid calls or network are used by tests.

## Offline evidence

Eleven grouped checks run actual fake CLI subprocesses in temporary roots: exact arguments and environment exclusion (including own-property __proto__ selection); complete binding/freshness rejection; source change during readiness; blocked readiness; effect then failure; overflow/timeout; wrong kernel and duplicate completion; strict config/JSON; bounded stdin; generic secret-free standalone errors. Tested on Bun 1.3.5/macOS arm64. These fixtures do not prove installed native CLI/provider compatibility or semantic correctness.

During implementation an initial fixture exposed /var versus /private/var canonical configuration paths; configuration paths now canonicalize before evidence membership checks. A second failing mutation fixture had already changed the task to the same bytes; it was corrected to introduce a distinct readiness-time edit without weakening the revalidation assertion. Both checks now pass. No existing CLI source, account service, lab ledger or provider code was changed.

## Optional private diagnostic receipts

Set `receiptDirectory` to an **existing, absolute, canonical directory with mode 0700**, or leave it absent/null to disable receipts (the example defaults to null). The adapter never creates that directory. It creates a new uniquely named mode-0600 JSON file per invocation, limited to 64 KiB, including failed invocations when the destination can be trusted. Keep this directory outside selected task/evidence paths so writing a receipt does not itself invalidate source identity or expose diagnostic state to the executor. Review any destination before enabling it; no upload or telemetry is performed.

Receipts include schema, attempt (or null for invalid attempt), start/end timestamps, last phase, status, whether native execution was launched, and available configuration/kernel/executable/image digests plus selected model/harness. Phases are config, input, image-policy, readiness, revalidate, run and completion; the image-policy failure code is NATIVE_ACTOR_IMAGE_POLICY_FAILED. Failure codes are fixed `NATIVE_ACTOR_<PHASE>_FAILED`; receipt-write failure raises `NATIVE_ACTOR_RECEIPT_FAILED`. A launch flag means the adapter attempted execution; it is not proof that an external effect occurred or did not occur.

Raw prompts, input evidence, environment values, native stdout/stderr, final text and child errors are not collected. A successful receipt still says native completion only. Disabled mode writes no receipt. Malformed/unreadable configuration or an invalid destination cannot establish a trusted receipt location, so these failures may have only generic stderr. Malformed stdin is recorded as an input-phase failure if configuration supplies a valid receipt destination.

Receipt writing is exclusive-create, bounded, mode 0600 and file-fsynced; directory identity/permissions are rechecked before writing. No full power-loss/hostile-filesystem guarantee is supplied. A write/fsync/destination failure makes the executor fail even after native success, preserving the outer pending-action requirement. A failed write can leave a partial diagnostic file; it must not be treated as a valid success receipt. This design intentionally prioritizes retained uncertainty over silently reporting success without required diagnostic persistence.

Offline receipt checks cover all seven phases, effect-then-failure, successful metadata, disabled mode, missing-directory refusal, mode 0600, no prompt/key/child-output leakage and receipt-write failure after execution. No additional provider calls were made.

### Correction to earlier readiness interpretation

Earlier review text incorrectly called the retained `/private/tmp/imp014-final-release-smoke/prose-bun` and `prose-rust` fixed-image builds. Root's later real doctor inspection reported imageSource=published-on-run for both. Their successful dry-runs proved only the then-resolved readiness selection. They did not establish fixed-image admission. The previous interpretation is withdrawn, and both artifacts are rejected by this new image-policy gate. Old evidence remains retained rather than rewritten as a successful fixed-image test.

### Selected-kernel binding before effects

The admitted image profile is deliberately narrow: exactly one payload at the internal image path `payload/kernel.md`. The public helper `kernelImageSha256(bytes)` accepts UTF-8 kernel bytes as Uint8Array/Buffer and computes the CLI aggregate SHA256 over the concatenation of `payload/kernel.md`, a NUL byte, the decimal byte length, another NUL, the exact kernel bytes, and a final NUL byte. This is byte framing, not Markdown parsing.

Snapshot validation requires this computed aggregate to equal expectedImageSha256 **before any doctor, readiness or action process is launched**, and checks it again during pre-action revalidation. Doctor then proves the digest-pinned CLI declares the same fixed image; completion independently checks delivered kernel bytes. An arbitrary multiple-payload image or a different internal payload path is unsupported, even if it is otherwise a valid CLI image. The selected kernel's standalone SHA256 and its framed aggregate image SHA256 are different identities and must not be interchanged.

Tests now include Unicode byte-length framing and mismatched selected-kernel/image rejection with zero process calls. Fake native fixtures derive their image identity from the same synthetic kernel bytes instead of using unrelated constant hashes. This closes the earlier possibility of a correct doctor image that did not correspond to the assessed local kernel.
