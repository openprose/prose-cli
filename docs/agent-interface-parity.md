# Existing account command machine-interface boundaries

Account and package commands accept rendering globals (`--output`, `--no-color`
and `--verbose`). Execution-only globals do not apply to them and must be
rejected before credential access or account side effects. Both implementations
emit the existing service-operation envelope with `INVOCATION_INVALID`, exit 2,
null result and empty stderr in JSON mode.

A provider-free audit compared 54 command/global combinations against compiled
Bun/Rust products at the integrated Codex-compatibility tree. Three cells showed
Rust silently ignoring `--harness`, `--timeout` or `--cwd` on account status,
while Bun rejected them. Shared controls now cover all 18 execution globals
against five existing account/package commands and retain rendering positives.
A malformed credential fixture establishes that rejection precedes fixture
parsing; a separate valid fixture checks the allowed signed-out response.

The Rust regression fails on the unchanged source and passes after adding the
rendering-only boundary. Bun passes the same independently frozen controls
without a product change. This fixes a concrete existing interface discrepancy;
it does not redesign discovery or qualify a live service or model route.
