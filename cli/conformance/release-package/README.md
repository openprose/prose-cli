# Release-package invariant corpus

This frozen, provider-free corpus checks only the installed CLI surfaces that are
safe in a release build: identity/help output, operational reports, and refusal
of the compiled-out mock harness. It is not the Phase-7 corpus, a language test,
or release/publication authority.

The admission runner derives expected version, source revision, build profile,
and image identity independently from protected inputs and the closed package
manifest. All three installed surfaces run the same ordered cases. npm must be
byte-for-behavior equivalent to the Bun standalone surface. Windows packages
receive static archive/npm/sidecar validation only; candidate execution remains
blocked until native Job Object containment is admitted.

Admission report version 3 binds the exact bytes of `invariants.v2.json` and
retains each normalized structured projection rather than a pass label. The
corpus `providerCalls: none` field states the intended provider-free case design;
the report truth is `providerCalls: not-observed`, because this local boundary
does not possess authoritative network or billing telemetry. POSIX reports also
retain the resolved Node and npm executable paths and byte digests used for the
offline installation, then reauthenticate those bytes after all case execution.
Windows records those tools as unobserved because its
candidate-execution boundary stops before installation.

The current v2 corpus retains the same eight ordered cases and schema format,
with an exact new help identity for the Codex compatibility option. The frozen
v1 file remains historical; replay its help binding using the matching source
revision, such as [the pre-change source](https://github.com/openprose/prose-cli/tree/8f27071140ccd71c59318c6cdce47adaecc6460e).
Current admission uses v2 only. This revision does not revise historical reports
or grant any additional release or publication authority.
