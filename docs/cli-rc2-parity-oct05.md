# CLI release parity audit — October 5, 2026

Published `@openprose/prose-cli@0.15.0-rc.2` contains the latest merged CLI
runtime at the audited main snapshot `39c90f454c1586d83e686e8b1438f113b5b96c3a`.
Its runtime source is `fe8b50328d87a9f60f3bbf2d527edffa56f3bd12`, runtime tree
`297158580f84c02aa90fd780bcd0315f53e1ce6c`. The seven changed paths since that
source are publisher workflow/code/tests, ownership, the RC2 release inventory
and maintained release documentation. No production runtime changed.

[Machine-readable audit](cli-rc2-parity-oct05.json) retains branch dispositions
and fresh public artifact checks. This is a dated snapshot; later runtime merges
require a new parity decision and unused immutable release version. This audit
made zero paid model calls and leaves the completed RC2 allocation closed.

## Branch and worktree coverage

The inventory covers all 70 remote heads, 69 heads in the task's clone and 33
canonical local heads: 172 ref records, 83 unique tips. Of these, 162 refs are
main ancestors or covered by merged PR heads, including squash integrations.
Two Git common repositories contain 30 registered non-bare worktrees; all were
clean, including untracked files, at inspection. Owner paths are omitted from
the public record. No canonical or sibling checkout was modified or cleaned.

The remaining ten ref records reduce to four categories:

| Branch | Disposition |
| --- | --- |
| `codex/imp-014-release-oct02` | Superseded release preparation records; integrated publication PR32/34/35/36/37 and distribution receipts govern RC2. No runtime delta. |
| `codex/imp-033-cli-docs` | Documentation-only Weave role alignment; optional owner review, no validated production CLI runtime left behind. |
| `codex/imp-082-cleanup-diagnosis` | Historical diagnostic workflow/instrumentation. The actual OpenMP cleanup fix merged in PR30 before the release source; separate historical Bun timing remains unexplained. |
| `codex/imp-083-prime-result-continuation` | Draft PR33, pending owner CI and focused qualification. Excluded from RC2. |

## Pin the finance demo

```sh
npm install -g @openprose/prose-cli@0.15.0-rc.2 --ignore-scripts
```

Use the explicit version and record the resolved executable/source/hash. A
mutable tag, stale checkout or an executable found through PATH alone does not
establish the demo identity. npm selects the Bun implementation. Fresh public
macOS ARM64 downloads passed version/source/kernel-profile/mock-disabled offline
probes with zero provider requests:

| Implementation | Version banner | Executable SHA-256 |
| --- | --- | --- |
| Bun | `prose 0.15.0-rc.2 (bun)` | `c236e27b2fc8c70287d29d1ad75ea083dcecbcf671407a927ad17717dbea962a` |
| Rust | `prose 0.15.0-rc.2 (rust)` | `ed0678295481f9f32db3977fa7e07433ce6aff7dffd3adbf6c0cb2942312dff1` |

The mirror manifest SHA-256 is
`87f70073e4dda83a71f7488773b04655d08783cd17d7f36477160a346a94d5ee`.
The freshly fetched npm root archive matches the original plan, SHA-256
`40642373d15fd9fb626e0fd71fc4593def62fb0e03b19a0f71a68ea2ea8c0322`.
Archive hashes, sizes, URLs and offline probe receipts are in the JSON record.
The complete October 2 signatures and publication verification remain in
[immutable publication custody](https://github.com/openprose/openprose-expedition/tree/6bea65bc6908db299e74dbbcd3e912309d3e8f6b/imp-014-npm-release-oct02/publication/0.15.0-rc.2).

Kernel selection is a separate pin. The freshly fetched public `kernel.md`
remains qualified `0.1.0-rc.1`, SHA-256
`f4a133bd41f8bf42433ccf26dc48440b6ec692512e04caaa1687d42e5ec2f345`.
Core main `28e8bb1a` and lab main `ed5dd26e` are newer source/research identities;
the corrected candidate is not yet behaviorally qualified or publicly promoted.
Historical finance kernel `9f8d6754` belongs to failed qualification candidate
`0.2.0-rc.1`; preserve it as a historical specimen. Private fixed-image builds
must retain their own image/kernel/configuration and executable identities;
the released CLI source alone does not prove published executable equality.

## PR33 review boundary

[PR33](https://github.com/openprose/prose-cli/pull/33), reviewed at
`0c7af8a39007028de19aa70dbe081543b4c0224b`, handles omitted result notifications
only after an observed assistant tool declaration and execution start/end.
The bounded path requires exactly one declared tool and no other open message
or reported result. It subsequently compares the omitted position with real
final `agent_end` history: call identity, name, content, details, error and any
observed partial result must agree. Unknown fields and invalid timestamps are
rejected; ordinary history, queue and terminal settlement checks remain.
Segment reset follows corroborated closure.

Independent Bun/Rust structural review found no concrete guard or projection
defect. Missing declarations or execution starts are rejected before this rule;
they are outside its narrow recovery scope. Final history is corroboration from
the same producer, not independent authentication. Synthetic or controlled
omission success cannot establish full finance or documentation workflow
qualification. The finance owner retains focused testing, source CI retry and
conditional integration. At this snapshot the PR is draft/unqualified and the
original macOS source-admission check failed. No merge, consumer upgrade or new
release is authorized by this audit record alone.
