# IMP-061 current CI qualification

Raymond's hosted-service and Claude compatibility changes, together with the
provider-free corrections recorded in the sibling qualification documents,
are on main at `6d7e8eb2ced840d443489593d29ca5f058090bd7` through
[PR 12](https://github.com/openprose/prose-cli/pull/12). PRs 10 and 11 are merged.
Fresh main CodeQL run 36777773864 passed; no alert was manually dismissed.

This follow-up replaces the missing, retired alpha workflows with structural
admission of the four maintained workflows. Full source admission runs on
Linux x64 and macOS ARM64; distribution rehearsal and unsigned kernel candidate
construction cover all four supported native targets. Public publication remains
manual, restricted to main and the publication environment. No package is
published by source or packaging CI.

The source repair adds the generic SDK's missing provider-free oracle scenario,
fixes its invalid isolation labels, and retains its distinct unmeasured status.
It removes Undici's builder filename from compiled Bun diagnostics without
changing the explicit transport. Installed rehearsal expects the current 50
shared cases. Strict Rust formatting and Clippy remain required.

Unused alpha promotion, post-publication alpha verification, frozen alpha registry
lineage and historical evidence enrichment are retired. They remain available in
Git history at the integration revision above. Current publication and package
custody contracts stay in admission. Missing historical live records are replaced
in ordinary tests by explicitly synthetic matrix and capture records; these do
not claim new live qualification or restore legacy evidence formats.

## Initial qualification checkpoint

Current workflow policy: 13 tests pass. Adapter oracle: 16 tests pass. Release
rehearsal contract: 16 tests pass. Compiled Bun standalone: 6 tests pass. Strict
Rust Clippy passes with test seams. The full provider-free admission run and
fresh remote matrices are still pending; this record will be updated with the
actual reviewed revisions and outcomes in the subsequent checkpoints below.

No paid provider invocation, package publication or deployment is assigned.
Release prerequisites, including npm package ownership/trusted publishing and
platform signing, remain separate gates for the future release.

## Subsequent findings

Run 36782446830 passed unsigned kernel candidate qualification on all four
platforms. Distribution run 36782446725 passed on macOS ARM64/x64 and Linux
ARM64, but its Linux x64 developer tests exposed Darwin-only assumptions. Source
run 36782446728 also caught a diagnostic string accidentally reformatted by a
lint repair. Both failures are retained; neither is counted as qualification.

Installed tests now distinguish recipe-supported hosts from unsupported-host
refusal. Unit process fixtures explicitly inject the admitted Darwin ARM64
identity, while compiled adversaries use their actual host. The four-harness
adversary builds isolated echo fixtures rather than substituting ordinary
published-kernel candidates or making a network/provider call. SDK oracle tests
remain separate from that installed cohort.

The adversary exposed OMP envelope-mode tool admission and Codex launch parity
mismatches. Both products now follow the shared recipe and native-mode contract.
Process cleanup uses a distinct typed process resource so it cannot collide with
the hosted service's strict not-found resource object. Current packaging tests
retain generated archive/npm custody, remove tests of deleted historical guide
examples, and expect the new service account readiness and Undici inventory.

Two open CodeQL findings have source repairs: test newline mutation replaces all
newlines; the experimental checkpoint lexer advances linearly through escaped
strings after JSON grammar validation. A fresh scan must verify closure; no
finding has been manually dismissed. All new qualification is still pending.

The remaining unused numbered-alpha package admission and draft assembly helpers
are retired rather than repaired to preserve their frozen alpha journey. No
maintained workflow invokes them. Current kernel assembly, publication verifier,
release-package admission, generated package guidance/custody and installed
rehearsal retain the relevant release protections.

Fresh source run 36786514562 exposed the Windows helper toolchain target being
requested after admission disabled the network. Source preparation now preloads
that declared target; a workflow negative test rejects its omission. The exact
release-package help identity is refrozen to Raymond's current service commands
(7,486 bytes, SHA-256
`d58587215fa0a5182433a81763c3a70370a92f7926c83f0b4187069025656096`).
The package account-status assertion uses the current service envelope and zero
exit for logged-out status; the default unavailable-harness doctor still exits
10. Two fresh installed-package machine-surface checks pass.

The last local benchmark gate depended on another absent historical migration
record. Those historical assertions are retired; current fixture collection now
checks manifest digests, byte-identical reanalysis and privacy. That check caught
absolute image/program/validator paths in exported identities. Admission still
verifies exact input bytes, but public descriptors now use portable paths. All
34 current benchmark contract tests pass. The fixture process is explicitly not
a CLI performance measurement.

Linux source run 36787737698 reached Rust tests and exposed three Linux Secret
Service assertions still using the former account envelope and unstructured
malformed-key reason. Those tests now follow the current service envelope and
structured credential fields, retaining secret-tool call, shadow resistance,
missing-D-Bus and recovery checks. This is a test update, not a legacy fallback.
Fresh Linux execution remains required.

The clean local run begun at runtime-source revision
`9c83400402a0e229b8415910fef249c1bff54ebd` passed all 53 provider-free
gates. It includes 717 Bun tests (714 pass, 3 recorded/platform skips), both
normal/developer Rust suites and strict lint, 17 compiled adapter adversaries,
1,089 hosted cases and 21 registry cases per product, exact-package custody and
the three-surface 50-case rehearsal. Subsequent edits are Linux-only credential
assertions and adapter host selection; runtime sources are unchanged. Focused
format and both strict Clippy builds pass after the Linux test update.

Fresh CodeQL run 36787732733 completed all four analyses and PR findings are
empty. Native Linux source admission and the final head matrices remain pending.
OMP's Linux environment-key isolation and early-EOF fixture checks remain active
even though Prime-specific native cases are not admitted on Linux.

Native Linux source run 36789532647 passed the credential tests and then found
two remaining core tests expecting config validation before host admission.
Both now assert the shared unsupported-host error and zero discovery on Linux;
admitted hosts retain the original model/auth validation assertions. 297 Linux core tests passed in that attempt, with two explicitly ignored;
the repaired suite needs fresh Linux qualification. No production validation order is changed.

## Integrated main qualification

PR 13 is merged at `313a76a3cc5e28fe69540f749627ba2304834375`.
Main CodeQL [36791191091](https://github.com/openprose/prose-cli/actions/runs/36791191091)
passed all four analyses; no open CodeQL findings remain and none were dismissed.
Unsigned candidates [36791191982](https://github.com/openprose/prose-cli/actions/runs/36791191982)
and distribution [36791192000](https://github.com/openprose/prose-cli/actions/runs/36791192000)
passed all four native platforms, including the three-surface 50-case rehearsal.
Linux distribution passed all 1,089 hosted cases in each product and 304
developer Rust core tests.

Full Linux source admission [36791192006](https://github.com/openprose/prose-cli/actions/runs/36791192006)
passed Rust but found nine Bun registry tests using Darwin's `/private/tmp`,
which does not exist on Linux. Their fresh workspace now resolves the native
temporary directory before creating it, retaining symlink refusal assertions.
This changes only the test fixture; no registry behavior or credential policy
is altered. Fresh native source qualification remains required.

## Linux packaging fixture deadline

On main `698ec3fa07865e3e8673aa5d8d8930a0f1bacbfc`, CodeQL and
all four candidate and distribution jobs pass. Full macOS source admission
also passes. Linux passes the Rust suites, 711 Bun tests (six platform/record
skips), compiled adversaries and both hosted corpora, but
[run 36793064314](https://github.com/openprose/prose-cli/actions/runs/36793064314)
stops in `LocalPackagingTests.setUpClass`: the full packager exceeds its
30-second fixture subprocess deadline. This is distinct from the outer
30-minute package-local gate budget.

A [retained compression-only Linux observation](validation/imp-061/package-compression.json)
uses the actual main-run ARM64 executables: 78,838,080 Rust bytes and
99,340,323 Bun bytes. The unchanged level-nine archive writer took 4.793,
4.052 and 4.061 seconds for the Rust, Bun and repeated npm payload; total
probe time was 13.018 seconds. Repeated Bun archives were byte identical.
This probe does not time product admission, snapshots, runtime inspection or
tool receipts, and does not reproduce the x64 runner's speed. It establishes
that substantial fixture work is archive compression; the three-minute bound
still needs a fresh native full-suite pass to establish the failure is repaired.

Only the two complete packaging-fixture subprocess bounds change from 30 to
180 seconds, with elapsed-time diagnostics on completed invocations. Smaller
image, compiler, npm-install and negative-probe deadlines are unchanged.
Release compression, executable bytes, custody/identity assertions and the
outer gate's settlement and time bounds remain intact. This is test setup,
not a relaxed publication or artifact-validation rule.

The revised local packaging suite completes 78 tests in 64.846 seconds,
with two explicit platform skips. Initial complete fixture packages take
6.80 and 6.38 seconds on local macOS ARM64. All current archive reproducibility,
snapshot custody, installed-package, tampering and refusal assertions pass.
Linux full source admission remains required after integration.

## Reader fixture synchronization

The fresh macOS source job in [run 36797343661](https://github.com/openprose/prose-cli/actions/runs/36797343661)
failed `malformed_protocol_retains_diagnostics_that_arrive_while_readers_settle`:
the protocol error was classified correctly but the expected late stderr was
empty. The fixture emitted malformed stdout before forking and detaching its
writer, allowing supervisor termination to race writer creation.

The revised fixture uses a readiness pipe before emitting malformed stdout.
The detached writer produces its diagnostic only when the supervised parent's
SIGTERM handler releases it; the writer also has a two-second bounded wait and
exits on pipe closure. There is no arbitrary delayed-write sleep. Both exact
ProtocolMalformed and stderr assertions remain. Production supervisor code is
unchanged. All 22 local fake-harness integration tests, formatting and strict
supervisor Clippy pass. The selected case passes 100 consecutive executions
without retries or skips; [the observation](validation/imp-061/reader-fixture-stress.json)
binds the candidate test bytes, toolchain and limits. Fresh native qualification
remains required. The preceding main Linux source run continues so the packaging
fixture's new budget can be evaluated independently.


## Native packaging fixtures

The complete Linux packaging suite in [main run 36797343661](https://github.com/openprose/prose-cli/actions/runs/36797343661)
now reaches all 78 tests: initial packages take 22.49 and 22.53 seconds under
the 180-second bound. Four assertions then fail because two direct release
invocations omit the required native readelf argument, and the snapshot-custody
fixture supplies scripts where the Linux packager requires native ELF inputs.

The fixture passes the authenticated readelf input and builds native C
stand-ins on both Linux and macOS for the custody case. The copied executable
mutates its original source file, preserving the check that the immutable
snapshot's packaged bytes do not change. Release profile and test-seam
refusal assertions remain exact. Production packaging and ELF/glibc checks
are unchanged. PR 16 combines these fixture corrections with reader
synchronization; fresh main native source and all matrix results are required
before completion.

The corrected local suite passes all 78 tests in 64.310 seconds with two
explicit platform skips, including snapshot mutation and release refusal.
