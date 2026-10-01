# OpenProse CLI implementation status

Updated: 2026-10-01

## Current source

Raymond's hosted-service and Claude compatibility changes, plus the corrective
proxy transport, schema and shared compatibility cases, are integrated into main
through [PR 12](https://github.com/openprose/prose-cli/pull/12), revision
`6d7e8eb2ced840d443489593d29ca5f058090bd7`. Both original PR histories are
preserved. Fresh main CodeQL run 36777773864 passed.

[PR 13](https://github.com/openprose/prose-cli/pull/13) integrates current source
admission and release-path cleanup. [PR 14](https://github.com/openprose/prose-cli/pull/14)
corrects Linux registry fixture paths. [PR 15](https://github.com/openprose/prose-cli/pull/15)
records the bounded full-packaging fixture repair and its native CI results.
[PR 16](https://github.com/openprose/prose-cli/pull/16) corrects reader
synchronization and native packaging fixtures without changing runtime code.
[PR 17](https://github.com/openprose/prose-cli/pull/17) repairs benchmark
collection for shallow profile paths while retaining diagnostic privacy.
Qualification is tied to exact source revisions and retained workflow results;
see [the repair chronology](../../docs/ci-roll-forward.md). Earlier green runs
do not qualify changed runtime sources. The shared installed corpus contains
50 cases.

Rust and Bun implement the same shared observable contracts independently.
Hosted commands cover account, registry, wallet, jobs and runs. Public clients
use the production service; custom service endpoints require explicit developer
builds. Installed process adapters forward opaque runtime images and tasks
without parsing OpenProse, opening a TUI, or silently changing a harness,
transport, credential source or billing owner.

Current adapter fixtures cover Prime, OMP, Codex, Claude and the generic Agents
SDK JSONL harness. The SDK fixture verifies an opaque transport contract; it
does not promote that harness into the measured four-harness inventory.
Claude admission accepts the documented version floor and ignores unknown
compatible telemetry while retaining terminal/session checks.

## Maintained admission and release path

There is one [maintained release path](../release/README.md). Full provider-free
source admission runs on Linux x64 and macOS ARM64. Distribution rehearsal and
unsigned kernel candidate construction cover Linux x64/ARM64 and macOS
x64/ARM64. Fresh installations exercise Rust, Bun and npm surfaces. Source and
packaging jobs are read-only and cannot publish.

Ordinary published-kernel startup, explicit fixed-image development packaging,
and sentinel transport tests remain distinct. The sentinel cannot enter public
artifacts. Passing mechanical CI establishes neither language semantics nor
live reliability. The CLI remains an outer runner; canonical language inputs
and semantic authority belong to their owning packages.

The manual publication workflow is main-only, serialized, and attached to the
`publication` environment. It verifies a reviewed plan and exact draft bytes,
platform trust, package identity and signing before publication. Every npm
package needs its own owner/trusted-publisher setup; first platform publication
requires explicit bootstrap setup. Stable macOS release still requires signing
and notarization. No publication or deployment is assigned by this CI repair.

## History and limits

Old alpha promotion, frozen registry lineage, direct legacy-skill experiments,
and in-place evidence enrichment are retired. Their implementation and records
remain in Git history; they are not supported release flows.

Historical source-workspace live collections used nonsemantic `echo-v0`
candidates. Their private observations and aggregate reports were not carried
into this repository. Current ordinary tests generate explicitly synthetic
capture and matrix records. Those tests validate custody, schema and privacy
rules without claiming a new live collection. New release bytes need their own
reviewed evidence, including any explicitly authorized paid qualification.

Current POSIX settlement tracks the direct process, bounded readers and original
process group; it does not claim containment of detached descendants. Native
Windows runtime admission remains separate and disabled in current public
builds. Neither these source checks nor an unsigned RC supplies stable-release
or publication authority.
