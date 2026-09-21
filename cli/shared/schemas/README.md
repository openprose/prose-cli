# Shared runner schemas

These Draft 2020-12 JSON Schemas are the normative data contracts for the
outer OpenProse runners. They deliberately do not define OpenProse source,
task-envelope semantics, or the language-owned terminal envelope.

All objects are closed unless a field is explicitly documented as opaque.
Opaque values are transported and hashed, never interpreted by the runner.
Schema identifiers are stable HTTPS identifiers; local file names are the
canonical repository copies.

Run the contract checks from the repository root with:

```sh
python3 cli/shared/tests/test_contracts.py
```

The test dependency is pinned in `../requirements-test.txt`.


Service and registry reports include `environment: production|staging` without
human banners in machine output. `service-environment.schema.json` describes the
persisted user selection; `package-operation.schema.json` describes publish,
fetch, list, and withdraw reports. Package results contain validated publication
receipts or one public listing page, never local destination paths or credentials.
The bounded listing cursor is opaque to callers and at most 210 ASCII characters.

Package envelope, canonical bytes, hashes, and receipts are defined by
[`../fixtures/registry/FORMAT.md`](../fixtures/registry/FORMAT.md) and its checked-in
vectors. A schema match alone does not establish artifact integrity: fetch also
requires hash, canonical-byte, reference, visibility, and inventory agreement.
Directory authoring manifests reject unknown and duplicate keys before upload.
These contracts do not establish backend deployment or permission to publish.
