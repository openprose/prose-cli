# Wallet

`prose cli wallet` reads and funds the prepaid OpenProse wallet. Every command needs an API key ([credentials](credentials.md)). Output carries only prices the service reports (`*_cents`, `*_dollars`, `*_usd`); the CLI never computes a price.

| Command | Request | Confirmation |
| --- | --- | --- |
| `cli wallet balance` | `GET /wallet/balance` | none |
| `cli wallet events [--limit N] [--before CURSOR]` | `GET /wallet/events` | none |
| `cli wallet usage [--start YYYY-MM-DD] [--end YYYY-MM-DD]` | `GET /wallet/usage` | none |
| `cli wallet redeem --code-file FILE\|-` | `POST /wallet/redeem` | `--yes` (money) |
| `cli wallet topup --amount-cents N [--idempotency-key UUID]` | `POST /wallet/topup` | `--yes` (money) |

```sh
prose cli wallet balance --json
prose cli wallet events --limit 50 --json          # then --before <nextBefore> for the next page
prose cli wallet usage --start 2026-09-01 --end 2026-09-30 --json
printf '%s\n' "$CODE" | prose cli wallet redeem --code-file - --yes --json
prose cli wallet topup --amount-cents 500 --preview --json
prose cli wallet topup --amount-cents 500 --yes --json
```

## balance

`result.balance` has `available`, `posted` and `reserved`, each as `_cents` (integer) and `_dollars` (a `"12.34"` string). `available = posted - reserved`: `reserved` holds money for runs in progress, and a cancelled or detached run can keep its hold reserved for up to 15 minutes. The service's `customer_id` and `*_nanos` fields are not shown.

## events

One page of ledger events, newest first: `id`, `type`, `occurred_at`, `description`, `amount_cents` (negative for charges), `balance_after_cents` and `ref` (a run id, `null`, or another service reference). `--limit` is 1 to 100 (default 20, always sent). `result.nextBefore` is the opaque cursor for the next page (`null` on the last page); pass it back unchanged with `--before`. An unknown cursor is `SERVICE_REQUEST_REJECTED` with `serviceCode: invalid_cursor`. An empty page exits 0 with `events: []` (human: `No wallet events.`). Human output prints every amount in dollars (`-$0.02  balance $33.37`) and a full `Next page:` command; `--output jsonl` prints one `openprose.service-record/1` line per event and a page line carrying `nextBefore` and the `note` in `meta`. Control characters in descriptions are shown as spaces; `note` is the service's informational note (Stripe receipts are the authoritative payment record).

## usage

Totals and a per-day breakdown (`runs`, `input_tokens`, `output_tokens`, `price_cents`) for a date window. Without `--start`/`--end` the service uses the last 30 days (UTC), from 30 days ago to today. Dates must be real calendar days written as `YYYY-MM-DD` (so `2026-02-30` and `2025-02-29` are rejected), and `--start` must not be after `--end`. When you give only one bound, the service fills in the other: a lone `--start` must not be after today (UTC), and a lone `--end` must not be before 30 days ago. Each of these mistakes is `INVOCATION_INVALID` (exit 2), not a retryable service error, and nothing is sent. Human output prints prices in dollars and `No runs in this period.` when there are no daily rows.

## redeem

A code given as an argument (`cli wallet redeem CODE`) exits 2 without echoing it; the suggestion reads it from standard input: `printf '%s\n' "$CODE" | prose cli wallet redeem --code-file - --yes`.

Redeems a one-time credit code the customer was given. The code is read from `--code-file` (or standard input with `--code-file -`), never from argv, with surrounding whitespace trimmed; it must be one line of at most 64 characters. The code never appears in output, and the confirmation plan (`CONFIRMATION_REQUIRED` or `--preview`) omits the body digest (`bodySha256` and `bodyBytes` are `null`) because a short code's digest could be reversed offline.

The result is `{ok: true, amount_usd, already_redeemed?, credit_pending?, available_usd?, message?}`. A redeemed code, including one already redeemed to this wallet, returns 200. A `202` response (`credit_pending: true`) means the code is claimed and the credit will appear shortly. The service gives one answer for every unusable code: `SERVICE_REQUEST_REJECTED` with `serviceCode: invalid_or_unavailable_code` (exit 10). Too many attempts return `SERVICE_UNAVAILABLE` (`rate_limited`); wait a minute.

## topup

Starts a Stripe Checkout session for `--amount-cents` of credit and prints its URL. The CLI never opens a browser: a person opens the URL and pays, and the credit lands once Stripe settles the payment (check `wallet balance`). The service enforces the minimum and the fee; a request below the minimum is `SERVICE_REQUEST_REJECTED` with the service's `serviceMessage`.

- The plan (`--preview` or `CONFIRMATION_REQUIRED`) shows the amount as `plannedRequest.summary.amount_cents`; human output prints `Summary: amount_cents=500 ($5.00)`. `CONFIRMATION_REQUIRED` says in `details.reason` that paying the Checkout page charges the payment method.
- Every top-up sends an `Idempotency-Key`. Without `--idempotency-key` the CLI generates a UUIDv4 when the request is actually sent (never for `--preview` or `CONFIRMATION_REQUIRED`). The key is echoed as `result.idempotencyKey`, and on any failure after sending as `problem.details.idempotencyKey`.
- To retry safely (a timeout, a `502`), rerun with `--idempotency-key <that key>` and the same amount. The service returns the same Checkout session instead of creating another. The same key with a different amount is `SERVICE_WRITE_CONFLICT`.
- `--idempotency-key` must be a lowercase UUID; `--amount-cents` is a positive whole number of cents (`500` is $5.00). `--amount 5` exits 2 and suggests `--amount-cents 500` (dollars converted to cents); `--amount 5.50` is not converted, and the reason states the unit. `cli wallet topup 500` suggests `--amount-cents 500`.

The result is `{checkout_url, session_id, amount: {credits_cents, fee_cents, total_cents}, idempotencyKey}`. The Checkout URL appears only in this result. Human output prints the URL, the amounts (in dollars) and the key on stdout.

## Errors

Beyond the per-command cases above, the usual [classification](../../cli/SPEC.md) applies. When redeeming or topping up is not available for the account, the command reports `SERVICE_FEATURE_DISABLED`. A malformed service response is `SERVICE_PROTOCOL_INVALID`, and `details.reason` names the field. Invalid options (`--limit 0`, `--amount-cents 5.00`, a bad date) are `INVOCATION_INVALID` (exit 2) and send nothing.
