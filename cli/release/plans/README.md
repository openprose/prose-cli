# Reviewed CLI publication plans

[0.15.0-rc.1.json](0.15.0-rc.1.json) is the reviewed plan for the published
unsigned prerelease. Its source, qualification evidence and bytes are immutable.
It does not qualify current main or authorize a new candidate.

`cli/ci/publication.py` verifies the exact inventory, four platforms for both
implementations, the root and four npm payload artifacts, and retained qualification.
New schema-2 plans use one npm registry identity with exact platform-suffixed
versions and dependency aliases; schema-1 plans retain their original identities. An
explicit RC may use the owner-authorized `unsigned-rc` policy; stable macOS
publication requires Developer ID signing and notarization. Synthetic fixtures
and development plans cannot authorize publication.

See [publication setup](../../../docs/cli-publication.md) and
[next-candidate preparation](../../../docs/cli-release-next.md). Adding a new
plan requires review of the exact source, final bytes and immutable evidence,
followed by approval of that concrete release. The retired functional-alpha
publication flow remains historical.

[0.15.0-rc.3.json](0.15.0-rc.3.json) binds source
`1941a34c3503f9a1417aebea1bc19ecada91e58c`, all eight standalone/five npm
archives and retained qualification evidence. All four platforms pass exact
Homebrew installation rehearsal; both MacARM binaries pass one Codex/OpenAI
Hello World each. The original macOS source-admission failure remains retained
alongside its single successful rerun. This is an unsigned prerelease smoke
qualification; Apple signing and the separate historical Bun cleanup diagnosis
remain open. No stable/latest or kernel promotion is authorized.
