# wallet cases

Shared cases for `cli wallet balance|events|usage|redeem|topup`. Case rules are in
[`../README.md`](../README.md); the user contract is [docs/service/wallet.md](../../../../../docs/service/wallet.md).

What this directory pins:

- **Projections.** Balance, events and redeem results drop `customer_id` and `*_nanos` fields.
  Free text is sanitized, and malformed identifiers are `SERVICE_PROTOCOL_INVALID`.
- **Paging.** `limit` is always sent. `next_before` is returned as `nextBefore`, and the cursor
  stays opaque.
- **Redeem.** The code comes from `--code-file` or stdin and never appears in output. The plan
  carries no body digest.
- **Top-up.** Every POST sends `Idempotency-Key`. A minted key is created only when the POST is
  sent and never for `--preview` or `CONFIRMATION_REQUIRED`. The key is reused verbatim with
  `--idempotency-key` and echoed in the result and in post-send failures.
- **Classification.** Covers the wallet error bodies, including `amount_capped`, which is not
  allowlisted as a `serviceCode`.
