# OpenProse CLI support

Use the route that matches the request:

- Report a reproducible installation, packaging, command, or existing-adapter
  problem with the [OpenProse CLI bug or install problem](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-bug.yml)
  form.
- Propose a new adapter, admitted harness version, or model route with the
  [OpenProse CLI harness or model request](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-harness-model.yml)
  form.
- Propose a benchmark profile or cell with the [OpenProse CLI benchmark profile
  or cell proposal](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-benchmark-profile.yml)
  form.
- Report a suspected vulnerability only through [private vulnerability
  reporting](https://github.com/openprose/prose-cli/security/advisories/new).

Do not include credentials, tokens, account identifiers, private paths, or raw
provider output in a public issue. Reduce diagnostics to the minimum sanitized
CLI output needed to reproduce the problem. The forms ask for exact versions
because alpha compatibility is an allowlist, not a version-family promise.

Maintainers triage reports as availability and project priorities permit. A
report may be redirected or closed when it is a language question, an upstream
harness problem, a request for an unsupported combination, a duplicate, or not
safely reproducible. No response or resolution service-level agreement is
promised.

## Troubleshoot the functional alpha

Keep the first reported stable error code and exit code. Diagnose that attempt
before changing the harness, version, configuration, or installation. The CLI
does not fall back to another harness or credential route.

### Start with the exact executable and machine output

Do not use an ambient `prose` executable. Set `PROSE` to the exact standalone
binary or npm launcher that reported the problem, then collect the following
structured reports locally. These commands do not start a model run:

```sh
PROSE='/absolute/path/to/the/selected/prose'
test -x "$PROSE"
"$PROSE" --version
"$PROSE" --output json cli config explain
"$PROSE" --output json cli harness list
"$PROSE" --output json cli doctor
```

A nonzero exit can still accompany a schema-valid JSON report. Read the stable
error code, corrective action, detected version, and configuration sources.
Do not publish these reports unchanged: replace credentials, tokens, account
identifiers, private paths, raw provider output, and unrelated configuration
values with `REDACTED`.

### Harness discovery, versions, and authentication

- For `HARNESS_UNAVAILABLE`, install the selected harness by following the
  OpenProse package's packaged README, confirm that its executable is on `PATH`,
  and run `cli doctor` again through `"$PROSE"`.
- For `HARNESS_INCOMPATIBLE`, use only the exact `Repair` command printed by the
  error or packaged README. Nearby versions are not assumed compatible. Run
  `cli doctor` again before another model invocation.
- For Codex, complete sign-in with `codex login`. For Claude, use
  `claude auth login`.
- For Prime and OMP, start the installed `prime-agent` or `omp` separately,
  complete its interactive sign-in or configuration, and exit it. OpenProse CLI
  never invokes or controls either TUI. Persist the selection with a fully
  qualified model and the explicit `prime-harness-login` or `omp-harness-login`
  profile. Only the actual run can confirm that authentication works; `doctor`
  cannot establish the provider, account, or billing route.

Never place a credential value in a CLI argument, config file excerpt, or issue.

### Configuration precedence conflicts

Run `cli config explain` through `"$PROSE"` and inspect the source reported for
each selected value. Precedence is invocation flag, `PROSE_*` environment,
project config, user config, then built-in default. In particular, check
`PROSE_HARNESS`, `PROSE_MODEL`, and `PROSE_AUTH_PROFILE`. Remove an unintended
higher-precedence project or environment override, or select the same harness;
do not claim that `cli harness use` changed the active default while another
source still overrides it.

### Runtime, platform, and package integrity

- The npm launcher requires Node.js 22.22.3 or newer. Linux packages require
  glibc 2.34 or newer. Windows and Linux with musl are unsupported by this
  functional alpha. Use the harness or model request for a new platform or
  combination; changing a version check does not make it supported.
- Do not bypass a package-integrity refusal or execute the rejected binary.
  Follow the packaged README to verify the release checksum and repair the same
  exact version from verified package bytes. Use a fresh standalone repair
  root when instructed. If the verified repair is refused again, keep the stable
  error text and use the bug form.

### Prime cleanup after a failed run

For `PROCESS_CLEANUP_FAILED`, use only the opaque cleanup handle reported by the
failed invocation and the same exact `PROSE` executable. Preserve the same
`TMPDIR` or platform temporary-root environment and run:

```sh
CLEANUP_HANDLE='paste-the-opaque-handle-from-the-error'
"$PROSE" --output json cli cleanup prime "$CLEANUP_HANDLE"
```

Do not stop an unrelated Prime service, search for another socket, or substitute
a different handle. If cleanup is interrupted during final removal, retry the
same command with the same handle and environment.

### Protocol failures and retries

`PROTOCOL_MALFORMED` is not retryable. `PROTOCOL_TRUNCATED` permits one explicit
manual retry. The wrapper does not perform a hidden retry; retry the latter
once only, preserve the failed attempt, and report the problem if it recurs.
Before a retry, run `cli doctor` through `"$PROSE"` and confirm the exact admitted
harness version. Never attach raw provider output to the report.

### Decide whether to repair or report

- Repair when the CLI or packaged README supplies an exact command for the
  selected supported platform, harness, and version. Re-run the machine
  diagnostics before making another provider call.
- Use the bug form when an exact supported installation still fails after the
  applicable repair, when package integrity is refused again, or when a protocol
  failure remains after its permitted manual retry. Include the CLI and harness
  versions, operating system and architecture, Node or glibc version when
  applicable, stable error code and exit code, a minimal command with private
  values replaced, and only the relevant sanitized machine fields.
- Use the harness or model request for an unsupported platform, harness version,
  authentication route, or model route. Use the benchmark proposal for a
  benchmark profile or cell. Use private vulnerability reporting instead of
  either public form for a security issue.

## Functional-alpha compatibility

This section describes the compatibility boundary for an authorized functional
alpha. It does not claim that an alpha artifact or package is currently
published; the CLI README is the availability authority.

Supported package and harness combinations are intentionally narrow:

| Package platform | Admitted harnesses |
| --- | --- |
| macOS Apple silicon | Prime, OMP, Codex, Claude |
| macOS Intel | Codex |
| Linux x64 with glibc 2.34 or newer | Codex, OMP |
| Linux ARM64 with glibc 2.34 or newer | Codex |

The Bun standalone and npm packages require macOS 13 or newer on macOS. The npm
launcher requires Node.js 22.22.3 or newer. Each package's README records the
surface-specific boundary.

The exact admitted harness versions are Prime `0.7.0` and `0.8.1`, OMP `18.0.9`
with Bun `1.3.14` or newer, Codex `0.149.0-alpha.4.1`, and Claude `2.1.243`.
The package and its packaged README remain the authority for a particular
alpha.

Supported here means that the package target and listed harness combination
have the functional alpha's mechanical admission. It does not claim semantic
OpenProse execution, strict containment, provider-account identity, billing
identity, or reliability.

### Best-effort environments

Linux distributions other than the retained Ubuntu 22.04 execution environment
are best effort even when they provide glibc 2.34 or newer. Maintainers may
triage a clear report from another environment, but the report does not extend
the compatibility claim or establish a new supported target.

### Unsupported environments and versions

Windows, Linux with musl, other operating systems or architectures, and harness
combinations absent from the table are unsupported. Nearby harness versions are
not inferred compatible; the CLI detects and rejects versions outside the exact
allowlist rather than silently continuing.

Functional-alpha support may be withdrawn when an upstream version, security
condition, protocol, or package target no longer meets the admission boundary.
An earlier alpha may be superseded by a later alpha, which may change the
supported platform, harness, version, or repair set. Availability of older
bytes is not a long-term support promise.
