# organizations cases

Shared black-box cases for the organization commands (`org show`,
`create`, `rename`, `default`, `member list|role|remove`, `invite`,
`invitation revoke|accept`). `org list` is covered by the account cases.

Expected documents were written from the frozen contract (the manifest,
`shared/schemas/service/organizations.schema.json`, `shared/errors/taxonomy.v1.json`
and the service's documented HTTP behavior), never captured from a product.
Groups:

- `<command>-confirmation-required` / `<command>-preview`: every mutation, no
  exchange;
- `<command>-yes` and friends: exact request bodies and closed projections;
- classification, control-character and token-handling cases.

See [`../README.md`](../README.md) for the case format.
