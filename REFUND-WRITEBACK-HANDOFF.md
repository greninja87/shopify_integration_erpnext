# Refund write-back — handoff

State at 2026-09-07, contract **version 4**. `main`, clean. **586 tests passing.**

```bash
python -m pytest shopify_integration/tests -q
```

Three documents carry the reasoning; this file is only the map.

- **`REFUND-WRITEBACK-BRIEF.md`** — why the feature exists, the Shopify evidence,
  the GraphQL shapes. Read §1 first: an earlier draft claimed this cannot move
  money and was wrong. §10 step 2 now carries a **correction** — see below.
- **`REFUND-DISPATCH-CONTRACT.md`** — contract **v4**, the interface
  `payment_portals` builds against for the **payout** (ERPNext → Shopify, moves
  money). §3 (routing), §5 (failure states) and **§2b (the state this app
  accepts)** are the load-bearing parts. §2b is new in v4 and it inverted the
  old gate — read it before assuming a `Completed` refund is dispatchable.
- **`REFUND-REPORT-CONTRACT.md`** — contract **v1**, the interface for the
  **report** (Shopify → ERPNext, moves nothing). §2 is the load-bearing part:
  this app never concludes a settlement channel.

## The two directions are not symmetrical

Keeping them apart is the whole safety story, so the difference is worth stating
once:

| | write-back (`utils/refund.py`) | report (`utils/refund_report.py`) |
|---|---|---|
| direction | ERPNext → Shopify | Shopify → ERPNext |
| **pays a customer** | **yes, on success** | never — no mutation on any path |
| hook | `refund_payout_dispatchers` (**registered**, v4) | `shopify_refund_observers` (unregistered) |
| gated on `enable_refund_writeback` | yes, and it is `0` | **no, deliberately** |
| worst failure | a customer paid twice | a refund ERPNext never hears about |

The report exists for what `payment_portals` cannot see, and only that. It does
**not** create a Payment Entry: `payment_portals` already detects the Cashfree
refund on its own, and two recorders for one refund is two Payment Entries. What
it cannot detect is a refund with no Cashfree row at all — #6518's ₹12,999 NEFT,
and every Snapmint refund. Confirmed read-only on the test site, 2026-09-07:
`#6491` has a Gateway Transaction refund row (`144073385-…`, ₹46,952.16,
Cashfree); **`#6518` has none, and never will.**

## It is inert, deliberately

| | |
|---|---|
| `enable_refund_writeback` | `0` on both stores |
| `doc_events` for Refund Request | none |
| `refund_payout_dispatchers` | **registered** (v4) |
| triggers | the **Refund in Shopify** button via `writeback_now`, and `payment_portals`' Send step via the hook |

**The toggle is now the only switch in front of a real payout**, which it was
not before v4 — the unregistered hook was a second one, by accident. Nothing
sends a refund without a person acting on a submitted `Shopify`-channel refund in
`Approved` or `Queued`, in a store whose toggle somebody turned on. There is
still no `doc_events` and nothing automatic.

## Outstanding

1. **Redeploy `electrobotictest`** to pick up `payment_portals` `ce0f69f` and
   this app's current `main`. Marker: the pre-flight returns
   `contract_version: 3` once both are in step.
2. **`REFUND-WRITEBACK-BRIEF.md` §10 step 2** — the already-refunded-order probe.
   The only live exercise that cannot move money. Needs a person.
3. ~~Confirm the hook name~~ **done in v4** — registered, and a test holds it
   to exactly one entry. `expected_amount` is still unbuilt if the cross-check is
   wanted — spec in contract §9.2, and a test refuses to let the parameter ship
   without its acknowledgement.

4. **Confirm the report hook name** `shopify_refund_observers` and the
   `consumed`-enumeration acknowledgement — `REFUND-REPORT-CONTRACT.md` §8.
   Nothing is registered on either side until `payment_portals` does.

Also unresolved: `order.transactions` needs one live response to settle
list-vs-connection. `transaction_nodes()` tolerates both meanwhile, and
`order.refunds` on the backfill path has the same open question and the same
tolerance.

## The assumption to keep in front of you

**Nobody has confirmed the Cashfree-OCC bridge fires for an API-created
refund.** Proven for a refund made by hand in the admin; assumed for
`refundCreate`. If it does not fire, this app reports `paid`, `payment_portals`
books it, and no money moves — the books say paid and the customer is not. That
is contract §9.5 and it is the reason the toggle stays `0` until one low-value
real order has gone end to end.

## The §10 probe does not do what §10 said

Corrected 2026-09-07, and it changes what items 1 and 2 above are waiting for.

The "safe probe" was described as exercising credentials, query, **mutation** and
error handling. It does not reach the mutation: `plan_refund` finds no parent
with headroom and `write_back_refund` refuses about thirty lines above
`refundCreate`. Shopify is never asked, so there are no `userErrors` to read and
the `Unverified`-before-post commit is never entered either.

Worse for the plan: the headroom guard is **strictly more conservative than
Shopify's**, so every order on which `refundCreate` actually posts is one Shopify
will accept — and a real customer gets paid. **There is no safe live exercise of
the mutation.** Both open items above assumed there was one.

The probe is still worth running for credentials, the `RefundTargets` query and
the live transaction shapes. It just cannot clear the mutation.
`TestTheSafeProbeNeverReachesTheMutation` pins this and was mutation-checked.

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
- **The dispatchable state is `Approved`/`Queued`, not `Completed`.** Requiring
  `Completed` meant booking the refund and *then* asking Shopify to pay it, which
  on an OCC order pays the customer after the books said they were paid. And the
  old channel test excluded only `Manual Portal Refund`, which left `Bank
  Transfer` writable — a NEFT refund paid a second time by the bridge. The gate
  is a positive allow-list now; do not turn it back into a list of exclusions.
- **`Unverified` is committed *before* `refundCreate` is posted.** That is what
  makes a killed worker safe; the flag that used to classify the failure was a
  local and died with the process. A clean rejection moves the row back to
  `Failed`. Do not "simplify" this into a post-hoc write.

Each has a test that fails if it regresses, and every one of those tests was
mutation-checked — a guard that cannot fail is the bug it is meant to catch.
