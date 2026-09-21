# Connect the CLI to staging

This feature is being developed under IMP-034. It is not included in an existing published release merely because this document exists. Use a candidate executable built from the same reviewed revision.

Staging is an explicit service environment. Local harness execution and model-provider credentials are separate from the OpenProse service account.

## Account connection

The intended command sequence is:

```sh
prose --service-environment staging cli auth login
prose --service-environment staging cli auth status --json
prose --service-environment staging cli org list --json
prose --service-environment staging cli auth logout
```

Login starts the existing service's GitHub device flow. Follow the verification URL and enter the displayed code in your browser. The CLI waits within the authorization expiry and reports cancellation or failure. Staging admission remains controlled by the backend's account allowlist.

Local credentials belong in operating-system credential storage, identified separately from production credentials and from credentials used by installed agent harnesses. If a supported credential store is unavailable, login must fail explicitly rather than save the key in plaintext. Logging out removes the selected local credential; it does not revoke every account session.

For automation, the staging-specific `OPENPROSE_STAGING_API_KEY` supplies a previously issued service credential. Supply it through your CI secret facility. It takes precedence over the local credential store. Do not put the value in shell history, source files, command arguments, or issue reports. Removing a local credential cannot unset a credential supplied by the parent environment.

## Scope

This connection supports account authentication and organization discovery. It does not enable hosted execution or contract publication. Registry upload and exact-version retrieval are separate IMP-034 delivery steps.

Status verifies an available credential through the organization API. That endpoint may create the account's default organization on first use. A missing credential is reported as signed out. A failed request must not silently select another environment, use a model-provider key, or fall back to local execution.

## Verification

Rust and Bun use the same shared conformance cases with a fake service. Those tests do not call GitHub, Cloudflare, or model providers. Live staging checks are explicit and use a separately provisioned account; browser authorization still requires the account owner's participation. Test credentials must not appear in retained output.

## Current platform limits

The candidate Rust implementation uses macOS Keychain through a bounded native helper. Its local credential-store operation is unavailable on other platforms; use the scoped staging environment credential there. Bun uses its native credential-store API where the operating system supports it. Cross-platform native-store parity has not yet been qualified. Neither implementation falls back to a plaintext file.

Native credential-store operations may request operating-system approval. If a store operation times out, inspect account status before retrying: an underlying operation that cannot be cancelled may complete after the CLI reports the timeout. Device login still requires browser approval; a hermetic login test does not prove that live approval path.
