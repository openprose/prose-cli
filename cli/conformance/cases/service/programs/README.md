# programs cases

Shared black-box cases for `cli program list|show|save|visibility|delete|revisions|draft`. Ids are prefixed `program-` because case ids are global across
features. Expected outputs were computed from the fixtures, the result schemas
in `shared/schemas/service/programs.schema.json` and the error taxonomy, not
captured from a product. See [`../README.md`](../README.md) for the case format
and [`docs/service/programs.md`](../../../../../docs/service/programs.md) for
the behavior they pin.

The 256 KiB save limit and the 1000-item listing bound are pinned by
runtime-generated process tests in both products
(`cli/rust/crates/prose-cli/tests/service_programs.rs`,
`cli/bun/test/service-programs.test.ts`) to keep large fixtures out of the
repository.
