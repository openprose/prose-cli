# Independent local integration review

## September 18, 2026: native local host and Bun coordinator

Read-only review by Codex `/root/weave_stress`; this file is the only review evidence edit. The reviewer authored the initial Bun coordinator and native actor, so independent execution/re-examination is not an independent security organization audit. No provider calls, actual model launches, credential-file reads, backend edits or Git operations occurred.

The reviewed native host and Bun coordinator preserve pending attempts after uncertain effects and do not replay them on restart in the tested scenarios. No concrete false-satisfaction, automatic pending replay or ambient credential exposure was found in this pass. This is a bounded finding, not a general safety proof. Both permit trusted capability processes; the capability's semantic correctness remains outside the host.

### Executed offline checks

Commands from the repository root (explicit paths were used in this review because cargo/Bun were not in the shell PATH):

```sh
cargo test --offline --manifest-path experiments/weave-seed/rust-local/Cargo.toml
/tmp/imp019-bun/bun-darwin-aarch64/bun --no-env-file experiments/weave-seed/local/coordinator.test.mjs
/tmp/imp019-bun/bun-darwin-aarch64/bun test experiments/weave-seed/providers/jev.test.mjs
```

Results at review time: native 10 tests passed; Bun local 12 checks passed; Jev 9 test groups passed. Native tests cover real separate-process duplicate ownership, config mutation, replacement-lock preservation, checkpoint corruption, actor failure, assessor rejection, unknown abstention, timeout/overflow/blocked input, explicit environment selection and idle SIGTERM. Bun checks cover corresponding local mechanics and TTL/source-gap/restart cases. Provider tests use mocked HTTP only; no key was sourced or request sent.

A native status projection initially included the storage schema while Bun's decoded diagnostic did not. The native owner independently fixed that discrepancy before this review's final rerun. It was a projection inconsistency, not a reset/replay failure. The reviewer also reported provider configuration path alias mismatch and stat-then-unbounded-read allocation risk. The owner canonicalized configuration/question identities and added bounded descriptor reads plus an alias regression; all 9 updated groups passed. Root requested and the native actor owner applied a separate null-prototype environment map with an own-property `__proto__` regression; all 8 native actor groups passed. These corrections are not hidden baseline successes.

### Concrete running-action cancellation comparison

An additional independent temporary-root probe used an actual Python capability. The assessor returned work-needed; the actor wrote an `effect` marker, then slept ten seconds. Both configurations had timeoutMs=800, maxAttempts=2, an empty selected environment and synthetic kernel/program/source files. Each actual CLI ran `serve CONFIG --poll-ms 1 --max-steps 2`. Once the effect marker appeared, the reviewer sent SIGTERM, captured the result, inspected checkpoint.json and then invoked `step CONFIG` again.

| Observation | Native Rust local | Bun local |
|---|---|---|
| Approximate elapsed after SIGTERM | 1 ms | 782 ms, approaching the synchronous child timeout |
| Step status | action-outcome-unknown | action-outcome-unknown |
| Stop reason | cancelled | pending |
| Stored attempts / pending | 1 / nonnull | 1 / nonnull |
| Service lock after orderly return | absent | absent |
| Restart | recovery-needed, no second effect | recovery-needed, no second effect |

These timings are observations, not deadlines guaranteed across systems. The native Unix handler can interrupt capability supervision and signal its process group. Bun cannot process the signal while spawnSync blocks; in this run it returned pending before a JavaScript signal handler reported cancellation. Thus claims of identical cancellation timing or identical stop reasons are false. The safety-relevant conservative pending behavior did match. Cancellation in both remains short of a hostile descendant/process-tree sandbox; separately detached descendants or external effects require reconciliation.

Reproduction capability body:

```python
import sys, json, pathlib, time
json.load(sys.stdin)
if sys.argv[1] == "assess":
    print(json.dumps({"judgment": "work-needed"}))
else:
    pathlib.Path("effect").write_text("applied")
    time.sleep(10)
```

Point assessor/actor argv at the same absolute Python executable and this file, adding assess/act respectively. Use fresh roots, wait for the marker before signaling, and compare its mtime after the recovery-needed restart. No cancellation attempt should be treated as proof that the first marker/effect did not happen.

### Ownership, bounds and claims

Both service owners hold the same service.lock name across idle polls and verify directory identity on release. The separate checkpoint host lock covers each step. This is cooperative local exclusion: calling older integration runConfig/FileHost directly bypasses service ownership. It is not distributed fencing, and no stale lock is automatically recovered. Uncertain checkpoint publication retains the underlying host lock even when a coordinator exits. Configuration byte changes stop subsequent service steps rather than silently change maxAttempts/policy during an owned session.

Rust process supervision uses direct argv, env_clear, explicit selected keys, nonblocking pipes and per-stream bounded capture. Bun uses direct spawnSync with explicit environment and its runtime's bounds. Their mechanism and cancellation differences must stay documented. Strict assessor grammar prevents final text/extra fields/escaped alternative judgment spelling from becoming satisfaction. Child zero exit permits reassessment, never directly proves satisfaction. Reused satisfaction still depends on the caller binding every relevant policy/question/program/source/obligation. Sequential filesystem observation is not an atomic snapshot.

Reviewed source fingerprints (working-tree byte identities, not release claims):

| File | SHA256 |
|---|---|
| `rust-local/src/lib.rs` | `2faa10e81ca50371225cb101f7555a58308e8278d179e7f7dcd22ccc5b0e070f` |
| `rust-local/src/process.rs` | `86c79c74ae7ec69b276083fd81c2c97af0ce257d25c4b99c3fcfe298ebfb59d3` |
| `rust-local/src/main.rs` | `a21ea93d4a3648650a8f6c207548d11d89cb20b669e639edadc0f4890d5f8918` |
| `local/coordinator.mjs` | `90d901ffd86b24bdade5762ebf06e7c56de118ebdb8b8a67e49753e8b42eef2f` |
| `integration/run.mjs` | `17dbaf10211992e191295323e6b0e7a3faa1fdb960371e6f8c6750e909f1ef94` |
| `providers/jev.mjs` | `e3ba8104c552e98ce78751a6cbdf1daa624de66ec270bbe440a4924041039bba` |
| `integration/native-actor/actor.mjs` | `3627b0106ee9806db4e4f72c35f5188fcf2c0ebcadb981d9c2b2ba36197ee0da` |

## September 18, 2026: actual retained native CLI readiness

The next probe used the existing fixed-image executables `/private/tmp/imp014-final-release-smoke/prose-bun` and `prose-rust`, not fake CLI fixtures. Both ran **only --dry-run**, in a fresh temporary subject and isolated environment:

- PATH was `/private/tmp/imp008-harness-tools/node_modules/.bin:/usr/bin:/bin`.
- OPENAI_API_KEY was the literal fake value `FAKE-NOT-A-REAL-KEY`.
- HOME was the temporary root; LANG was C.UTF-8. No ambient environment, .env file, real credential or cached account was copied.
- program.md contained a synthetic instruction identifying the request as a dry run.

Exact argv shape, substituting each retained executable and the temporary ROOT:

```sh
BINARY --harness agents-sdk --auth-profile openai-api-key --model MODEL   --native-max-turns 8 --native-timeout 120000ms --native-tool-timeout 15000ms   --timeout 150000ms --cwd ROOT --output-contract native   --dry-run --output json run program.md
```

A separate 50-second subprocess timeout bounded each readiness check. Both exited zero with empty stderr. Observed identity and readiness:

| Field | Bun | Rust |
|---|---|---|
| executable SHA256 | fc19301da383e2ba6162ac71fc10bedcaf2c3af2f03b23e7a87dc8e05bfe540a | e27ac70183c4173aeb1bc8f4a8582dac5241324776975551dfc6a09bed9e1457 |
| readiness / wouldStartModel | ready / false | ready / false |
| harness / adapter | agents-sdk / agents-sdk/jsonl | agents-sdk / agents-sdk/jsonl |
| admitted runtime / model | prose-agents-sdk 0.1.0 / MODEL | same |
| prompt | system-append / strict | same |
| billing category | user-provider | user-provider |
| auth readiness | unknown | unknown |
| isolation / releaseEligible | partial / false | partial / false |
| image SHA256 | 6cd37fd568df61026688ca2e3f684dbf76e1a51fa8a67fbd832f6225bbb81cb7 | same |
| native limits | 8 turns, 120 seconds, 15 tool seconds, 12000 output tokens | same |

The new native actor's readiness conditions match these real outputs. Rust serializes nativeLimits in a different property order, so comparison must use fields and values, as the adapter does. Selection does not expose authProfile; an adapter must not require a nonexistent field. Explicit auth argv and reported billing category do not establish authenticated account identity. This dry run does not verify delivered kernel after an actual invocation, provider access, effects, spending, semantic fulfillment or fresh-install availability. It establishes readiness-contract compatibility for these exact retained artifacts and installed harness only.

No normal run was launched. The older artifacts are releaseEligible=false, and the installed SDK executable is machine-local. The known inability to override the ordinary CLI's runtime kernel selection remains a user-setup qualification gap: configured expected hashes fail closed on mismatch; a post-action delivered-image check cannot undo effects if published selection changes between readiness and execution. No readiness result here should be promoted into a live BYOK acceptance claim.

### Final native test expansion

After the owner added active-actor SIGTERM, descendant-held-pipe timeout, serving expiry/source mutation, and callback/help regressions, the reviewer repeated the same offline cargo command. All **14 native tests passed** (2.74 seconds reported test duration). This supplements the earlier 10-test result without replacing its history. The owner README now explicitly documents active-cancellation differences from Bun. No further blocking safety finding was observed in this bounded pass.

### Native actor diagnostic receipt addition

At root request, the native actor now accepts optional receiptDirectory (absent/null disables; existing canonical absolute mode-0700 directory required). Bounded exclusive mode-0600 file-fsynced receipts preserve fixed phase-specific failures without raw input, prompt, environment values, child output or provider error text. All six phases, effect-then-failure, disabled/missing destination and write failure after success are covered by the expanded **9 grouped native actor checks**, which passed with real fake-CLI subprocesses and no provider requests. The reviewer authored this addition; it is implementation evidence, not independent external approval. Invalid/unreadable configuration may lack a trusted receipt destination. Receipt persistence failure makes the actor fail and leaves the enclosing effect pending.

Updated working-tree byte identities supersede the earlier native actor fingerprint:

- `integration/native-actor/actor.mjs`: `33fb88381c8ac619df0eddba0c785941f53ff99ed7497477b5038681074c51a8`
- `integration/native-actor/run.mjs`: `2690fe2b98e2cde3f54a63bb20e8c05323f1b2b0d51b3781ce243f5bb8f1197f`
- `integration/native-actor/actor.test.mjs`: `e22c43c482fdeb51ee0543ea4b83bdc5b29fd7ca998417d3e3c3507eb25e079c`


### Additive correction: retained CLI builds were published-on-run

The preceding section “actual retained native CLI readiness” incorrectly described the two imp014-final-release-smoke executables as fixed-image. Root subsequently inspected their actual `cli doctor --json` output and found **imageSource: published-on-run** in both. The successful dry-run results and hashes remain accurate observations, but their interpretation as fixed-image readiness is withdrawn. These artifacts can change selected kernel content across invocations and are not admitted by the corrected native actor.

The native actor now invokes bounded local doctor before readiness and requires the exact doctor schema, no imageSource property, build.testSeamsEnabled=false, and the configured image SHA256. Any published/unknown source property, malformed schema, image mismatch or enabled test seam fails with NATIVE_ACTOR_IMAGE_POLICY_FAILED before readiness/action. This is admission of an explicitly pinned trusted executable's declared fixed image, not a sandbox or independent binary attestation. Actual fixed-image binary qualification is being performed separately by root; this addendum does not claim it has completed.

Ten grouped native actor tests passed after the correction, including exact doctor→dry-run→run argv, doctor rejection with only one process invocation, and image-policy private receipts. The full-loop Bun/Rust offline adapter test passed with the doctor-aware fixture; generated-loop's actual Bun test-runner case also passed with the real adapters and mock provider transport. An initial direct invocation of generated-loop correctly failed because it requires `bun test`; the corrected test-runner invocation passed. No model/provider calls occurred. Doctor adds a second readinessTimeoutMs deadline; defaults therefore require outer coordination greater than 45+45+165 seconds, with 270 seconds documented.


### Pre-effect selected kernel/image binding correction

Doctor aggregate equality alone did not establish that the local observed kernel matched the embedded payload before an effect. Snapshot validation now computes the documented CLI aggregate for the narrow single-payload profile `payload/kernel.md` from the actual selected kernel bytes and rejects an expected-image mismatch before **any** process launch. It rechecks before action. The exported helper kernelImageSha256 frames internal path, byte length and payload with NUL separators. Multiple-payload/arbitrary-path images are explicitly unsupported rather than guessed.

All 11 native actor groups passed, including zero-process-call rejection of mismatched image/selected kernel and Unicode byte-length framing. The full-loop test passed; generated-loop plus root-owned configure tests passed 6 groups. A read-only calculation against the retained actual kernel bytes produced `6cd37fd568df61026688ca2e3f684dbf76e1a51fa8a67fbd832f6225bbb81cb7`, matching the established canonical aggregate. No model/provider calls occurred. This is stronger admission than the earlier post-run delivered-kernel check and supersedes claims that doctor comparison alone closed the selected-kernel mismatch.
