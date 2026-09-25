# discovery cases

Shared black-box cases for `cli service status`, `cli service triage`,
`cli model list`, `cli example list|show` and `cli repo list` (see
[`docs/service/discovery.md`](../../../../../docs/service/discovery.md)).
Fixtures use service-shaped bodies from the 2026-09-24 probe. Expected
outputs were derived by an independent oracle from the frozen result schemas
(`shared/schemas/service/discovery.schema.json`), not captured from either
product.

Case id prefixes: `service-status-*`, `model-list-*`, `example-list-*`,
`example-show-*`, `repo-list-*`, `triage-*`. Defensive paths pinned here:
`example-show-cross-origin-refused` (a source outside the service origin is
never followed) and `repo-list-github-link-required` (a `/repos` 401).

`service status` and `service triage` are user-facing: they read only
`status`, `models` and `default_model` from `/health` (the status cases carry
extra `/health` fields and `forbid` them, so nothing else is ever printed), and
`model list` prints only the models `/models` lists, with the default, plus
the allowlisted `catalog` fields when the service sends one
(`model-list-catalog-*`).

The `example-show-output-*` cases pin the `--output-file` pre-check shared
with every `--output-file` command (`service/fs` `check_new_file` /
`checkNewFile`): no exchanges are supplied, so any request fails the case.
