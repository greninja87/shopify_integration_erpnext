# Refund report contract — `shopify_integration` → `payment_portals`

**Version 1.** Written 2026-09-07 by the `shopify_integration` side, as the
interface an observer builds against.

This is the **other direction** from `REFUND-DISPATCH-CONTRACT.md`. That one is
`payment_portals` asking this app to *make* a refund happen in Shopify, and a
successful call **pays a customer**. This one is this app telling ERPNext a
refund **has already happened** in Shopify. It moves no money, and it cannot:
there is no mutation anywhere on this path.

`shopify_integration.utils.refund_report.REPORT_CONTRACT_VERSION` is the
machine-readable version, and every result dict carries it as
`report_contract_version`. As with the dispatch contract, a version number is
**not** an acknowledgement — see §3.

---

## 0. Why this exists at all, and why it is not a recorder

`payment_portals` already detects the Cashfree refund on its own. That is
verified on production: all four Shopify-originated refunds arrived as Cashfree
refund rows and were linked to the right Sales Order with **no Shopify
involvement whatsoever**.

So this app deliberately does **not** build a Payment Entry, and must never be
made to. `payment_portals` is the only app permitted to create a ledger
document — asserted there by two live tests — and it already owns the refund
ceiling, the deductions, the accounts and the approval. **Two triggers for one
refund is two Payment Entries.**

What this app owns is what `payment_portals` cannot see:

1. **Refunds that never reach Cashfree.** #6518's ₹12,999 went out by NEFT.
   Snapmint refunds are not Cashfree refunds. No Gateway Transaction row will
   ever appear for either, so the detection that works for the others cannot
   fire, and **Shopify is the only source that knows the refund happened**.
2. **The Shopify refund's own facts** — refund id and gid, note, line items,
   restock, and which staff user made it. None of it is derivable from a
   Cashfree row.

Verified read-only on `electrobotictest`, 2026-09-07, and it is the whole case
for this seam in two rows:

| order | Gateway Transaction rows | the refund row |
|---|---|---|
| `#6491` | 2 | `144073385-7380e11d6e` — `event_type: Refund`, ₹46,952.16, Cashfree |
| `#6518` | **0** | **none, and there never will be** |

Same `manual` gateway in Shopify on both. Opposite settlement. Which is also
§2.

---

## 1. Coupling rules

Unchanged from the dispatch contract, and pointing the same way:

- `shopify_integration` **never** imports `payment_portals`.
- `payment_portals` **must not** import `shopify_integration`.
- The call goes through Frappe's hook fan-out, never a direct import.

The coupling is exactly two things: a hook name, and the facts-and-answer shape
below.

---

## 2. The conclusion this app refuses to draw

**`settlement_channel` is always `"undetermined"`. It will never be anything
else, and a test pins that across every gateway string.**

This is not caution for its own sake. Cashfree-OCC-Notdrones creates these
orders and marks them paid *manually*, so Shopify holds no gateway transaction
of its own and logs the refund against the `manual` gateway. Therefore:

- `#6491` — timeline reads *"manually marked ₹46,952.16 as refunded"*. That
  landed as **Cashfree refund 144073385 in the same minute**. Real money, real
  Cashfree refund.
- `#6518` — same `manual` gateway, went out **by NEFT**, no Cashfree row, ever.

**Identical Shopify evidence, opposite settlement.** The discriminator is
`Refund Request.portal_account -> provider`, which lives in `payment_portals`
and is invisible here. So this app hands over the raw `gateways` list verbatim
and an explicit "I do not know", and the decision stays with the app that can
make it.

> Never read `manual`, or a timeline reading "manually marked as refunded", as
> evidence that no money moved. That misreading has produced a wrong conclusion
> twice on this project.

---

## 3. The hook, and the acknowledgement it must return

### Registering

```python
# payment_portals/hooks.py
shopify_refund_observers = ["payment_portals.<module>.observe_shopify_refund"]
```

Nothing is registered by `shopify_integration` — it is the producer. Exactly
one observer is expected; **two is refused and nothing is sent**, because two
recorders for one refund is the two Payment Entries this design exists to avoid.

### Called as

```python
frappe.call(observer, **facts)      # facts as in §4
```

### The answer — and why a bool is not enough

`frappe.call` filters the caller's kwargs to the parameters the resolved target
declares. **An argument the target does not accept is dropped silently** — no
`TypeError`, no warning. That was proved by accident on `electrobotictest`
once already, and §1 of the dispatch contract turns it into a rule.

Here **every** field in `MUST_CONSUME` changes a decision. An observer that
never received `settlement_channel` will assume the ordinary Cashfree path and
leave a NEFT refund permanently unbooked — which is the exact backlog this
feature exists to fix. So the acknowledgement is neither a bool nor a version
number: **the observer must enumerate the fields it consumed.**

```python
def observe_shopify_refund(**facts):
    ...
    return {
        "shopify_refund_report_accepted": True,
        "consumed": ["shopify_refund_id", "shopify_order_id", "amount",
                     "gateways", "settlement_channel", ...],
        "recorded_as": "REF-00301",     # optional, for the log line
    }
```

`MUST_CONSUME` — every one of these must appear in `consumed`:

| field | why its silent loss matters |
|---|---|
| `shopify_refund_id` | identity; without it a redelivered webhook is a second refund |
| `shopify_order_id` | which order. The payload's own `id` is **not** this |
| `amount` | how much. There is no total field; it is summed here |
| `gateways` | the raw gateway — the only settlement evidence that exists |
| `settlement_channel` | the explicit "I do not know" that stops the misreading |

Declaring `**facts` is the simplest way to be safe against a future field, but
it is not sufficient on its own: an observer that *sees* a field and stays
silent about it is still unacknowledged. Silence is not consumption.

Adding a field to `MUST_CONSUME` **is** a version bump, because it makes every
existing observer unacknowledged until it claims the new field.

---

## 4. The facts

```python
{
  "report_contract_version": 1,
  "provider":       "shopify",
  "source":         "webhook" | "backfill",

  "shopify_refund_id":   "929361464",
  "shopify_refund_gid":  "gid://shopify/Refund/929361464",
  "shopify_order_id":    "7843650535529",
  "shopify_order_name":  "#6518",
  "shopify_store":       "notdrones.myshopify.com",

  "amount":            12999.0,   # summed from the successful refund txns
  "currency":          "INR",     # "" when the txns disagree
  "transaction_count": 1,
  "gateways":          ["manual"],       # verbatim, deduplicated

  "settlement_channel": "undetermined",  # always. See section 2

  "note":            "Customer cancelled, refunded by NEFT",
  "shopify_user_id": "799407056",        # the staff user who made it
  "created_at":      "2026-09-01T10:15:00+05:30",
  "processed_at":    "2026-09-01T10:15:00+05:30",
  "line_items":      [{"line_item_id": "...", "sku": "...", "title": "...",
                       "quantity": 1, "restock_type": "no_restock",
                       "subtotal": 11015.42, "total_tax": 1983.58}],
  "restocked":       False,        # derived from the lines, not the input flag
  "notify":          None,         # see below
}
```

Three of these are worth stating rather than inferring:

- **`amount` is summed, not read.** A refund payload carries no total. Only
  transactions with `kind: refund` and `status: success` count — a failed refund
  transaction moved nothing, and counting it would report a larger refund than
  happened.
- **`notify` is `None`, never `False`.** Shopify does not persist `notify` on
  the refund resource; it is write-only on the input. Reporting `False` would
  invent a fact, and a reader would take it as "the customer was not emailed".
- **`restocked` is derived from the line items**, not from the refund's
  top-level `restock`, which is an input flag and not reliably echoed. ERPNext
  is the inventory master, so this is for reconciliation, not for acting on.

---

## 5. The result

What `report_refund` returns, on every path. It **never raises** — it runs
inside a webhook that must return 200 about a refund that has already happened,
and a non-200 makes Shopify retry an event that is not the problem.

```python
{
  "provider": "shopify", "report_contract_version": 1,
  "outcome":   "reported" | "unacknowledged" | "no_observer" | "refused" | "failed",
  "delivered": bool,     # true for exactly one outcome: "reported"
  "message":   "<human sentence>",
  "unconsumed": ["settlement_channel"],   # what the observer did not claim
  "recorded_as": "REF-00301",
  "observer":  "payment_portals....",
  "shopify_refund_id": "...", "shopify_order_id": "...",
  "amount": 12999.0, "settlement_channel": "undetermined",
}
```

`delivered` is stated rather than left to the caller for the same reason
`retry_safe` is stated in the dispatch contract: this is the axis where getting
the mapping wrong means a refund silently goes unrecorded, and a boolean is
harder to get wrong than a string compare against a remembered set.

| `outcome` | means |
|---|---|
| `reported` | the observer took it and enumerated what it consumed. **The only delivered outcome** |
| `unacknowledged` | it answered without claiming every required field — dropped by `frappe.call`, or silent about it. **Nobody has been told**; goes to the Error Log |
| `no_observer` | nothing registered. Normal on a site without `payment_portals`, and deliberately **not** logged as an error |
| `refused` | the observer declined, or two are registered |
| `failed` | the observer raised. Logged with its traceback |

`unacknowledged` is the state that matters, and it is the safe direction for
absent, unknown and not-yet-implemented — the same principle as
`caller_must_pay` pointing the other way.

---

## 6. Entry points

| function | whitelisted | what it does |
|---|---|---|
| `report_refund_from_webhook(payload, shop_domain, shopify_order_name)` | no | the `refunds/create` path. Called by `api.py` |
| `backfill_order_refunds(shopify_order_id, shop_domain)` | no | reads `order.refunds` from Shopify and reports each |
| `backfill_now(shopify_order_id, shop_domain)` | **yes** | the HTTP door for the backfill, gated on Shopify Settings **write** |
| `report_refund(facts)` | no | the seam itself |

`backfill_now` is gated on a permission on **this app's own doctype**,
deliberately not on a role list borrowed from `payment_portals` — the same
reasoning that removed the permission check from `write_back_refund` in dispatch
contract version 3. It sends nothing to Shopify and cannot pay anyone, but it
does hand facts to another app that may record a refund from them.

## 7. What this side will never do

- never create a **Payment Entry**, Journal Entry or any other ledger document
  — pinned by a test that inspects every insert and every write;
- never write to **Refund Request**, including this app's own five write-back
  fields — a report is not a write-back;
- never post a **mutation** on the report or backfill path, so no path here can
  move money — pinned by a test asserting the backfill query contains no
  `mutation`;
- never gate reporting on **`enable_refund_writeback`**. That toggle guards the
  payout and is `0` on every store, which is where it must stay; gating the
  report on it would mean the one safe thing only ran while the dangerous thing
  was armed;
- never conclude a **settlement channel** from Shopify data. See §2;
- never **raise** from `report_refund` or `report_refund_from_webhook`.

---

## 8. Open

1. **Confirm the hook name `shopify_refund_observers`,** and the
   `consumed`-enumeration shape of the acknowledgement. Nothing is registered on
   either side until you do. If you prefer another name or a different
   acknowledgement key, say so and this side will change — the whole coupling is
   a hook name and a dict shape.
2. **`order.refunds` list-vs-connection is unsettled**, exactly as
   `order.transactions` is in the dispatch contract. `_refund_nodes()` tolerates
   list, `nodes` and `edges` meanwhile, and a test covers all three. One live
   backfill read settles it.
3. **A durable record of an `unacknowledged` report.** Today it reaches the
   Error Log, which is where `refund.py` puts an Unverified write-back and is
   enough for a person to act on — but it is not queryable, so "which Shopify
   refunds has ERPNext never heard about?" cannot be answered in a report. If
   that question needs answering, this app should own a small doctype for it
   rather than write to one of yours.
