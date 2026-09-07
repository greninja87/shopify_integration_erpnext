# Refund write-back — handoff

State at `de01e54` (2026-09-04). `main`, clean, pushed. **501 tests passing.**

```bash
python -m pytest shopify_integration/tests -q
```

Two documents carry the reasoning; this file is only the map.

- **`REFUND-WRITEBACK-BRIEF.md`** — why the feature exists, the Shopify evidence,
  the GraphQL shapes. Read §1 first: an earlier draft claimed this cannot move
  money and was wrong.
- **`REFUND-DISPATCH-CONTRACT.md`** — contract **v3**, the interface
  `payment_portals` builds against. §3 (routing) and §5 (failure states) are the
  load-bearing parts.

## It is inert, deliberately

| | |
|---|---|
| `enable_refund_writeback` | `0` on both stores |
| `doc_events` for Refund Request | none |
| `refund_payout_dispatchers` | not registered |
| only trigger | the **Refund in Shopify** button, via `writeback_now` |

Nothing sends a refund without a person pressing that button on a submitted,
Completed Refund Request in a store whose toggle someone turned on.

## Outstanding

1. **Redeploy `electrobotictest`** to pick up `payment_portals` `ce0f69f` and
   this app's current `main`. Marker: the pre-flight returns
   `contract_version: 3` once both are in step.
2. **`REFUND-WRITEBACK-BRIEF.md` §10 step 2** — the already-refunded-order probe.
   The only live exercise that cannot move money. Needs a person.
3. **Confirm the hook name** `refund_payout_dispatchers`. One line in `hooks.py`
   here, nothing else. Then `expected_amount` if the cross-check is wanted —
   spec in contract §9.2, and a test refuses to let the parameter ship without
   its acknowledgement.

Also unresolved: `order.transactions` needs one live response to settle
list-vs-connection. `transaction_nodes()` tolerates both meanwhile.

## Four traps, each already paid for once

- **`manual` gateway does not mean no money moved.** The Cashfree-OCC app bridges
  a Shopify refund into a real Cashfree one. A successful `refundCreate` pays the
  customer.
- **Branch on `caller_must_pay`, never `owns_payout`.** The latter is three-state;
  `null` is falsy, and `if not owns_payout: pay()` pays a refund Shopify may
  already have paid.
- **Guard order in `check_eligibility` is load-bearing.** Ownership is settled
  second, right after idempotency. Move it later and a zero-amount non-Shopify
  refund comes back caller-owned under the wrong code.
- **`Unverified` is committed *before* `refundCreate` is posted.** That is what
  makes a killed worker safe; the flag that used to classify the failure was a
  local and died with the process. A clean rejection moves the row back to
  `Failed`. Do not "simplify" this into a post-hoc write.

Each has a test that fails if it regresses, and every one of those tests was
mutation-checked — a guard that cannot fail is the bug it is meant to catch.
