# Build brief — Shopify refund write-back

**For the session building this in `shopify_integration`**
(`D:\Project\shopify_integration_erpnext-phase1\shopify_integration_erpnext-main`).

Written 2026-09-04 from `payment_portals` at `e7a090b`. Everything below is
verified against live Shopify data or the 2026-01 Admin GraphQL docs; where it is
not, it says so.

---

## 1. What this is — a payment instruction, not a record

Refunds are raised in ERPNext. For an order that came from Shopify, **ERPNext
tells Shopify to refund, and that is what actually pays the customer.**

> **This section was wrong in an earlier draft and was rewritten on 2026-09-04.**
> It first claimed the write-back could not move money, on the strength of
> Shopify's timeline wording. That was wrong. If you are reading a copy that says
> "this write-back will not move money", discard it.

Verified against production `Gateway Transaction` data. Every Cashfree-OCC order
refunded in the Shopify admin has a **real Cashfree refund** behind it:

| Shopify order | what Shopify logged | the real Cashfree refund |
|---|---|---|
| `#6491` | *"manually marked ₹46,952.16 as refunded"*, 31 Aug 5:02 pm | `144073385`, ₹46,952.16, **31 Aug 17:02:59** |
| `#6507` | *"manually marked ₹33,999.00 as refunded"* | `144358435`, ₹33,999.00, 2 Sep 15:58 |
| `#6515` | — | `144359702`, ₹30,999.00, 2 Sep 16:06 |
| `#6531` | *"manually marked ₹5.00 as refunded"* | `144011966`, ₹5.00, 31 Aug 10:53 |

`#6491` lands in the **same minute** as the Shopify action. The
`Cashfree-OCC-Notdrones` app is bridging Shopify's refund into a real Cashfree
refund.

**Why the wording misleads, and the mistake to avoid.** These orders are created
by the OCC/Snapmint apps and marked paid manually, so Shopify holds no gateway
transaction of its own and logs the refund against the *manual gateway*. That
says nothing about what the OCC app does next. **Never read "manual gateway" or
"manually marked as refunded" as evidence that no money moved.** An earlier
reading of exactly those words produced the wrong conclusion twice — including
about `#6531`, whose ₹5 refund did reach Cashfree, just later.

**Snapmint is the exception.** All 42 production refunds are Cashfree; there has
never been a Snapmint refund event. `#6518`'s ₹12,999 went out by **NEFT**
instead. So a Snapmint refund either does not flow through Shopify or is
invisible here — treat Snapmint as *not* refundable this way until proven
otherwise. (Enforced upstream in `payment_portals` by
`SnapmintProvider.supports_refunds = False`; see §11.)

**The design consequence, and the core of this brief:** copy the gateway off the
order's own parent transaction rather than hardcoding `"manual"` — that is what
the admin does, and it is the shape the OCC bridge recognises. But understand
that a successful `refundCreate` **is a payout**, and build every guard on that
basis.

## 1a. The double-refund hazard — read before writing any code

Because Shopify's refund reaches Cashfree on its own, ERPNext must **never** both
write back to Shopify and call the Cashfree refund API for the same request. That
pays the customer twice.

The split, decided by the user on 2026-09-04:

| the payment being refunded | money path | channel |
|---|---|---|
| has a Shopify order (`Sales Order.shopify_order_id` set) | **Shopify `refundCreate`** — the OCC app fans it out to Cashfree | `Payment Portal` |
| a payment link / direct Cashfree, no Shopify order | **Cashfree refund API**, unchanged — this is what it is for now | `Payment Portal` |
| Snapmint | NEFT or their portal, by hand | `Bank Transfer` / `Manual Portal Refund` |

So `payment_portals` **does** need one change after all: its Cashfree send path
must refuse a refund whose order came from Shopify, and vice versa. That change is
listed in §2a and is being made in that repo, not this one. **Do not build the
write-back to fire before that guard exists**, or a refund raised on the portal
channel will go out through both paths.

The existing ceiling arithmetic is a partial backstop — Cashfree will not refund
more than it captured — but two refunds of the same amount can both succeed while
headroom remains. Do not rely on it.

## 2. Placement decision

**Almost all code in `shopify_integration`; `payment_portals` changes only to
route the money path (§2a).**

- `shopify_integration` already holds the per-store credentials (Shopify
  Settings), a GraphQL client with throttling, retries and `userErrors`
  handling, and Shopify Log. Rebuilding that elsewhere means two credential
  stores.
- The dependency points one way only. `payment_portals` must keep working on a
  site with no Shopify app installed.
- **Do not `import payment_portals` either.** Read the Refund Request through
  `frappe.db` / `frappe.get_doc` by doctype name. That keeps the coupling to a
  data contract rather than a Python one, and `shopify_integration` stays
  installable without `payment_portals`. Guard every entry point with a
  `frappe.db.exists("DocType", "Refund Request")` style check so the hook is
  inert when the app is absent.

### Why owning the DocType does not mean owning the change

`payment_portals` defines Refund Request, so the instinct is that fields and hooks
for it must go there. They do not. Frappe lets one app extend another app's
DocType, and **`shopify_integration` already does exactly this three ways** for
DocTypes it does not own:

| need | mechanism | already used there for |
|---|---|---|
| new fields | `Custom Field` records via `install.create_or_update_custom_field(doctype, field_def)` | Sales Order, Delivery Note, Customer, Payment Entry, Sales Order Item |
| server-side reaction | `doc_events` in `hooks.py` | Sales Order, Delivery Note |
| form button / UI | `doctype_js = {"<DocType>": "public/js/….js"}` | Delivery Note |

The helper takes `doctype` as a plain argument, so `"Refund Request"` is just
another string to it. A Custom Field lives in its own table, not in
`refund_request.json`, so nothing in `payment_portals` is edited, and a
`bench migrate` there will not undo it.

### 2a. The one change that IS in `payment_portals`

Superseding an earlier draft that said none was needed. Now that a Shopify refund
is known to pay the customer (§1a), that repo must route the money path:

- The Cashfree send path must **refuse** a `Payment Portal` refund whose Sales
  Order carries a `shopify_order_id` — Shopify owns that payout.
- It must keep working exactly as today for payment-link and direct-Cashfree
  payments, which is now its purpose.
- The form should say which path a given refund will take, so the person
  approving it knows whether Shopify or Cashfree will move the money.

That work is happening in `D:\Project\payment_portals`. **This build must not be
enabled until it lands.**

**Two further places you could argue for a `payment_portals` change** — both
deliberately avoided, so decide knowingly rather than discover later:

1. **A dedicated domain event.** This brief hooks `on_update_after_submit` and
   tests `status == "Completed"`, rather than having `payment_portals` emit
   something like a "refund booked" event. The cost is that the hook re-runs its
   guards on every post-submit save; that is cheap, because the stored-GID guard
   short-circuits first. The benefit is that this stays one app's work.
2. **The daily digest.** `payment_portals/tasks.py` owns the exceptions digest.
   If a refund that failed to reach Shopify should appear there, that *is* a
   `payment_portals` change. Right now a failure lands in Shopify Log and the
   `shopify_writeback_error` field, and nothing chases it. Worth a decision.

**One side effect to record.** `frappe.get_meta` counts custom fields, so the
Refund Request field count changes once these install — `docs/HANDOFF.md` uses
"field count 50" as a live deploy marker for `e85761c`. After this ships that
marker reads 54, not 50. Update the handoff table rather than reading it as a
broken deploy.

## 3. The seam — what is already reachable

No new links are needed:

```
Refund Request.sales_order  ->  Sales Order.shopify_order_id   (custom field, this app)
                            ->  Sales Order.shopify_store      (custom field, this app)
```

`shopify_store` resolves to Shopify Settings via the existing
`utils/fulfillment._settings_for_store(shop_domain)` pattern. Reuse it or lift it
to a shared helper.

### Field mapping

| Shopify | Refund Request field | notes |
|---|---|---|
| `input.orderId` | via `sales_order.shopify_order_id` | wrap with `shopify_graphql.gid("Order", …)` |
| `transactions[].amount` | **`net_refund_amount`** | this is the figure `payment_portals` sends to the gateway (`actions/refund_execution.py:199`). **Not** `refund_amount` (gross, before deductions) and **not** `total_payout` (includes reimbursements). |
| `input.note` | **`reason_note`** | this is the **refund reason**. Fall back to `f"Refund {name}"` when blank. |
| `input.notify` | — | **`False`** by default. Put it behind a Shopify Settings toggle; ERPNext already emails, and two refund emails to one customer is a support ticket. |
| `refundLineItems` | — | **omit in v1.** See §8. |

**On the refund reason — do not go looking for a `reason` field.** There is no
separate one. `RefundInput.note` *is* the reason: the docs describe `refundCreate`
as issuing a refund and attaching "a note explaining the reason for the refund",
and it is what the admin's **Reason for refund** box writes. That is why `#6491`'s
timeline reads *"1 item, no reason provided"* — an empty `note`. So
`reason_note` → `input.note` puts the ERPNext reason exactly where a person
reading the Shopify order expects to find it.

`RefundInput` does also carry a `discrepancyReason`
(`OrderAdjustmentInputDiscrepancyReason`). **That is not this.** It categorises an
order-adjustment discrepancy, not the human reason for the refund. Leave it unset.

The admin's own refund dialog confirms the mapping and the field's audience.
On `#6601` (notdrones, unfulfilled, ₹27,000 refundable) it shows:

- **Reason for refund** — a free-text box captioned *"Only you and other staff can
  see this reason"*. So `note` is **staff-visible only**; it never reaches the
  customer. `reason_note` can therefore carry internal detail (the Refund Request
  name, the credit note) without worrying about who reads it.
- **Refund method: Original payment** — a dropdown.
- **Refund amount** with a single row labelled **`Manual`** and
  *"₹27,000.00 available for refund"*.

Two things worth taking from that last line. The per-row label is the parent
transaction's gateway — `Manual` again, on a fourth notdrones order — and
"available for refund" is `maximumRefundableV2` on that transaction, which is
exactly the per-parent cap `plan_refund` allocates against. The dialog is doing
what §5 describes; we are reproducing it through the API.

## 4. The GraphQL

Verified against the 2026-01 Admin GraphQL docs.

### Read the refund targets

```graphql
query RefundTargets($orderId: ID!) {
  order(id: $orderId) {
    id
    name
    transactions(first: 20) {
      id
      kind
      status
      gateway
      formattedGateway
      amountSet { presentmentMoney { amount currencyCode } }
      maximumRefundableV2 { amount currencyCode }
      parentTransaction { id }
    }
  }
}
```

**Watch the shape asymmetry:** `order.transactions` takes `first:` but returns a
plain list (no `edges`/`node`), while `refund.transactions` on the mutation result
*is* a connection with `edges { node { … } }`. Both forms appear in the official
examples. Verify against a real response on your API version before trusting
either — a wrong assumption here fails silently as an empty list.

### Create the refund

```graphql
mutation PortalRefundWriteBack($input: RefundInput!) {
  refundCreate(input: $input) {
    refund {
      id
      note
      totalRefundedSet { presentmentMoney { amount currencyCode } }
      transactions(first: 10) {
        edges { node { id gateway kind status
                       amountSet { presentmentMoney { amount } } } }
      }
    }
    userErrors { field message }
  }
}
```

Input:

```json
{
  "input": {
    "orderId": "gid://shopify/Order/7843650535529",
    "note": "<reason_note>",
    "notify": false,
    "transactions": [
      {
        "orderId": "gid://shopify/Order/7843650535529",
        "parentId": "gid://shopify/OrderTransaction/<parent id>",
        "kind": "REFUND",
        "gateway": "<the parent transaction's gateway, verbatim>",
        "amount": "12999.00"
      }
    ]
  }
}
```

`refundCreate` also accepts an `@idempotent(key: "…")` directive. Use it with a
key derived from the Refund Request (`name` + amount), so a retry cannot double
up. **Confirm the directive is accepted on the configured API version** — if it
is not, the stored-GID guard in §6 is the only protection, so do not skip that.

## 5. Module design — `utils/refund.py`

Follow the split `utils/fulfillment.py` already uses: pure decision functions
first, frappe-bound orchestration after. The pure half is where the correctness
lives and it must be testable with no bench.

### Pure

```python
def refundable_parents(transaction_nodes) -> list:
	"""Parent transactions a refund can attach to, best first.

	Keep kind in {"SALE", "CAPTURE"} with status "SUCCESS" and
	maximumRefundableV2.amount > 0. Everything else -- REFUND rows, VOID,
	FAILURE, AUTHORIZATION with nothing captured -- is not a parent.
	"""

def gateway_moves_money(gateway) -> bool:
	"""False for "manual" and for blank/None; True otherwise.

	Blank reads as manual because an absent gateway is not evidence of a real
	one, and "we are not sure money moved" is the safe answer.
	"""

def plan_refund(transaction_nodes, amount) -> dict:
	"""Allocate `amount` across refundable parents, capped per parent by its
	maximumRefundableV2.

	Returns {"transactions": [...], "gateways": [...], "moves_money": bool,
	         "allocated": float, "problem": str | None}.

	`moves_money` is True when ANY allocated parent's gateway moves money.
	Set `problem` and allocate nothing when the parents' combined headroom is
	short of `amount` -- a partial Shopify record is worse than none, because
	it looks settled and is not. Say both figures in the message.
	"""

def build_refund_input(order_gid, plan, note, notify=False) -> dict:
	"""The RefundInput. No refundLineItems -- see the restock note."""
```

### Frappe-bound

```python
def write_back_refund(refund_name: str, triggered_by: str = "manual") -> dict
def refund_writeback_on_update(doc, method=None)   # the hook; enqueue only
def writeback_now(refund_name: str) -> dict        # whitelisted, for the retry button
```

`write_back_refund` sequence:

1. Guards (§6). Return a `{"ok": False, "status": "Skipped", …}` shape rather
   than raising, so the caller can log it.
2. Claim the row — the `_claim` / `_set_state` / `_release_claim` pattern in
   `fulfillment.py`. Two workers must not both refund.
3. Resolve store → Shopify Settings; resolve `shopify_order_id`.
4. `RefundTargets` query → `plan_refund`.
5. `refundCreate` → **`check_user_errors(data, "refundCreate", context=refund_name)`**.
   This is the load-bearing call: HTTP 200 with empty `userErrors` and nothing
   done is the failure mode this codebase already has a test for.
6. Write the result fields (§7) and a Shopify Log entry either way.

## 6. Triggers and guards

**The trigger changed with §1, and this is the part still needing the user's
yes.** An earlier draft fired on `on_update_after_submit` when
`status == "Completed"`. That was right for a write-back that only *recorded*
something. It is wrong for a payout: `Completed` is set by **booking**, which
happens after the money has moved, so a passive hook there would be asking
Shopify to pay for a refund ERPNext already considers paid.

Since `refundCreate` is now the payout, it has to happen where the Cashfree API
call happens today — at the deliberate, role-gated **send** step out of
`Approved`, subject to the same ceiling arithmetic and `PAYOUT_ROLES` check.
Those all live in `payment_portals`.

**Recommended interface**, keeping the dependency one-way:

- `payment_portals` gains a dispatcher hook. When a `Payment Portal` refund is
  sent and its order is Shopify-backed, it looks for a registered refund
  dispatcher (`frappe.get_hooks(...)`) and delegates the payout to it.
- If no dispatcher is registered, it **refuses** rather than falling back to the
  Cashfree API — "cannot pay" is the safe direction, and it is the direction
  every other guess in that app's refund path already falls.
- `shopify_integration` registers the dispatcher and implements it with
  `write_back_refund`.

So `write_back_refund(refund_name)` should be written as a **callable payout
function returning a result dict** — which §5 already specifies — and *not*
wired to `doc_events` yet. Build it, test it, expose it whitelisted, and leave
the wiring until the `payment_portals` side of the interface exists. Everything
else in this brief is unaffected.

**Guards, all of them:**

| guard | why |
|---|---|
| `refund_channel == "Manual Portal Refund"` → **skip** | that refund came *from* Shopify already; writing it back duplicates it there |
| `shopify_refund_gid` already set → **skip** | idempotency; survives a retry, a requeue and an amended doc |
| no `sales_order`, or no `shopify_order_id` on it → **skip** | not a Shopify order (payment links, direct Cashfree) |
| store not resolvable, or its Shopify Settings disabled → **skip, loudly** | record it; do not fail silently |
| `net_refund_amount <= 0` → **skip** | nothing to record |
| a Shopify Settings `enable_refund_writeback` toggle, **default off** | see §10 — the first run writes to a live storefront |

**The credit-note loop.** `utils/credit_note.create_credit_note_from_shopify_refund`
runs off the `refunds/create` webhook. Our own write fires that webhook, and it
will make a second Credit Note for a refund ERPNext already has. **Guard this
before switching the write-back on**, by matching the incoming refund's GID
against `shopify_refund_gid` on any Refund Request and returning early. Store the
GID before the webhook can arrive, or the race will beat you — write it in the
same transaction that reports success, and commit.

## 7. Custom fields on Refund Request

Installed by **this** app, via `install.create_or_update_custom_field` (the helper
already there) plus a patch in `patches/`, modelled on
`patches/add_fulfillment_fields.py`. They are Shopify-shaped, so they do not
belong in the `payment_portals` doctype json.

| fieldname | type | purpose |
|---|---|---|
| `shopify_refund_gid` | Data, read-only | the returned refund GID; the idempotency key and the loop guard |
| `shopify_writeback_status` | Select, read-only | `""` / `Pending` / `Done` / `Failed` / `Skipped` |
| `shopify_refund_gateway` | Data, read-only | **the gateway Shopify actually used** |
| `shopify_writeback_error` | Small Text, read-only | last failure, for the retry button |

`shopify_refund_gateway` records which gateway Shopify attached the refund to.
**Do not describe `manual` as "no money moved"** — on these orders `manual` is the
normal value *and* the customer gets paid, via the OCC app (§1). Describe it as
what it is: the gateway on the parent transaction. The proof of payment is the
Cashfree refund that follows, which `payment_portals` already ingests through
Settlement Recon and matches by order reference — at a median lag of about 48
hours, so its absence proves nothing for a day or two.

**Set `no_copy: 1` on all four.** Refund Request is amendable — it has
`amended_from` — and `payment_portals` already treats carry-over as a bug worth
testing: `tests/test_refund_request_meta.py` has
`test_the_portal_s_own_answer_never_carries_over` and
`test_the_bank_utr_never_carries_over`, on exactly the grounds that a new document
must not inherit the old one's evidence. A `shopify_refund_gid` that carried into
an amended copy would make the guard in §6 skip a refund that was never written,
and it would fail *silently* — the worst direction. Add a test for it, in that
app, mirroring those two.

Also set `allow_on_submit: 1`, since every one of these is written after the
document is submitted.

## 8. Deliberately not in v1

- **`refundLineItems` / restocking.** Sending line items makes Shopify restock,
  and ERPNext is the inventory master here. An amount-only refund is a legitimate
  `refundCreate` (transactions with no line items). Revisit only with a decision
  about which system owns stock.
- **Shipping refunds** (`input.shipping`). Not modelled on the Refund Request.
- **Partial write-back** when headroom is short. Refuse instead; see `plan_refund`.

## 9. Tests to write

Mirror the existing naming (`tests/test_fulfillment.py`, `test_shopify_graphql.py`)
and the `frappe_stub.py` harness. Baseline before you start: **308 passed**.

Pure:
- `refundable_parents` drops REFUND, VOID, FAILURE and zero-headroom rows.
- `gateway_moves_money`: `"manual"` → False, `""`/`None` → False, `"cashfree"` → True.
- `plan_refund` caps per parent at `maximumRefundableV2`.
- `plan_refund` allocates nothing and sets `problem` when headroom is short, and
  the message carries both figures.
- `plan_refund` spreads across two parents when one is too small.
- `moves_money` is True if *any* allocated parent is a real gateway, False for an
  all-manual order (the `#6518` case — build the fixture from its real shape).
- `build_refund_input` emits no `refundLineItems`, and copies each parent's
  gateway verbatim rather than a constant.
- `build_refund_input` puts `reason_note` in `note`, and substitutes the fallback
  when it is blank or whitespace — so no refund reaches Shopify reading "no reason
  provided" when ERPNext had a reason for it.

Frappe-bound (stubbed):
- each guard in §6 skips and records a reason, and none of them raise.
- `userErrors` present → `Failed`, error stored, **no GID written**.
- HTTP 200 / empty `userErrors` / no refund object → `Failed`, not `Done`.
- success writes GID, status `Done`, and the gateway from the response.
- the credit-note guard returns early for a webhook whose GID we stored.
- a second call on a row with a GID does nothing.

## 10. Verifying it, safely

**There is no Shopify test store, and a successful call pays a real customer.**
The ERPNext test site (`electrobotictest.m.frappe.cloud`) carries real Cashfree
data, but a Shopify write from it hits the **live storefront** and, through the
OCC bridge, real money. Treat every call as a payout.

1. Ship with `enable_refund_writeback` **off**. Do not turn it on yourself.
2. First live exercise: an order that is **already fully refunded** (e.g.
   `#6518`, or `#6491`, both fully refunded). Shopify must refuse with a
   `userErrors` about exceeding the refundable amount. That exercises
   credentials, query, mutation and error handling, and it is the only step that
   cannot move money. Confirm the refusal, then stop.
3. Everything past that point moves customer money and belongs to the user, not
   to you. Hand them §10 and let them choose the order and the moment. `#6601`
   has ₹27,000 of headroom — precisely the sort of order not to experiment on.
4. Do not enable generally until **both** the credit-note guard (§6) and the
   `payment_portals` routing guard (§2a) are in place. Either one missing means a
   double refund or a duplicate Credit Note.

## 11. Open questions for the user

- ~~Whether a Snapmint order can be refunded through Shopify at all.~~
  **Closed 2026-09-04. No guard needed in `shopify_integration`.** The
  discriminator is `Refund Request.portal_account -> provider`, and
  `payment_portals` already enforces it: `SnapmintProvider.supports_refunds` is
  False, so `portal_channel_blocked` refuses a Snapmint portal refund outright
  with "make the transfer and record its UTR here". A Snapmint refund therefore
  never reaches the write-back on the portal channel. The transaction nodes could
  not have answered this anyway — OCC and Snapmint orders both read `manual`.
- A Shopify login with permission on `electrobotic-in` — its 3 refunds are still
  unexamined.
- Whether the customer should get Shopify's refund email (`notify`). Default off
  here.
- Whether Shopify should restock (see §8).
