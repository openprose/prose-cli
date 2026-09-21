# Service accounts and environments

The CLI connects to the production OpenProse account service by default. Local model providers and local execution retain their own configuration; selecting a service environment does not enable hosted execution.

```sh
prose cli auth login
prose cli auth status --json
prose cli org list --json
prose cli auth logout
```

Login displays a GitHub verification URL and code. Approve the request in your browser; the CLI waits for completion and saves the credential in the operating system credential store. Status verifies a stored credential against the service. Logout removes the local credential; it does not revoke the server credential.

## Use staging

```sh
prose cli environment use staging
prose cli environment show --json
prose cli auth login
prose cli environment reset
```

Selection persists in the existing user `cli.toml`. Reset removes the selection and restores production. `use production` saves an explicit production selection. These commands do not contact a service or change credentials. Account commands accept an ephemeral `--service-environment production|staging` before `cli`; this does not change the saved selection. Projects cannot select a service environment.

Staging service output is labeled `OpenProse staging`; JSON reports identify the environment without a banner. Each environment has an independent credential-store namespace. A request never falls back to another service or credential. Endpoints are fixed; there is no arbitrary destination override.

## Automation

Use `OPENPROSE_API_KEY` for production or `OPENPROSE_STAGING_API_KEY` for staging. Only the selected variable is consulted, and it takes precedence over the selected local credential. Login and logout refuse to modify credentials while that variable is active. Neither service key is forwarded to model harnesses, including through explicit inheritance settings.

Rust native credential storage currently supports macOS; other platforms can use the scoped environment variable. Bun uses its native credential-store API. No plaintext storage fallback is provided. OS approval may be required; a native operation that cannot be cancelled may finish after timeout.

## Qualification

Both implementations have hermetic service and persistence tests. Live browser approval and native-store interoperability remain unverified for this candidate. Current source changes are not a released CLI or a production deployment. See [candidate validation](validation/imp-034/README.md).
