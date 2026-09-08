# Refund report contract — `shopify_integration` → `payment_portals`

**Version 2.** Written 2026-09-07 by the `shopify_integration` side, as the
interface an observer builds against. Revised 2026-09-08.

> **Version 2 changes no acknowledgement and adds no *required* fact. It bumps
> because two other things an observer branches on did change** against version
> 1, and the version-1 reading of the bump criterion — "the facts dict and the
> acknowledgement protocol are unchanged, so no bump" — did not cover either of
> them.
>
> 1. **The `outcome` vocabulary gained a slug**, and this document invites you
>    to branch on it: `own_writeback`, for a refund this app raised and whose
>    Shopify response it read. It is a skip, it is not delivered, and an
>    observer never sees it — but a caller enumerating outcomes sees a string
>    version 1 did not list.
> 2. **A new obligation is on you: dedupe on `shopify_refund_id`.** Delivery is
>    at-least-once (§5a). That was true and unsaid under version 1, so an
>    observer built against it may not do it. An obligation is part of a
>    contract even when no field moved.
>
> The facts dict gained one **optional** fact,
> `unconfirmed_writeback_on_order` (§4). The result dict gained
> `refund_request`, which names the row a skip was decided against, and the
> same optional list. Nothing was removed. **An observer that acknowledges the
> `MUST_CONSUME` fields and dedupes needs no code change** — including one that
> never declares the new parameter, which `frappe.call` then drops.
>
> **Why this is still 2 and not 3.** A draft of version 2 carried a second
> outcome slug, for a report *withheld* when a write-back of ours on the order
> was unconfirmed. That was withdrawn before release: withholding turned an
> undecidable case into a permanent omission, and nothing re-drove a withheld
> report. Version 2 was never released to a consumer — so the only version
> anyone can have integrated against is 1, both numbered statements above are
> still exactly true of the difference from 1, and the two round-3 changes add
> no third thing to re-integrate for. Bumping to 3 would announce a
> re-integration for a slug no released build ever emitted and a fact nobody is
> obliged to read.

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
| `shopify_refund_id` | identity. Delivery is at-least-once (§5a), so **the observer must dedupe on `shopify_refund_id`** — without it a redelivered webhook is a second refund |
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
  "report_contract_version": 2,
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

  # OPTIONAL, and NOT in MUST_CONSUME. See below and section 5a.
  "unconfirmed_writeback_on_order": [],   # e.g. ["REF-00207"]
}
```

### `unconfirmed_writeback_on_order` — optional, and safe to ignore

The `Refund Request` rows of ours **on this order** whose write-back was posted
and never confirmed. Always a list, so "none" (`[]`) is a different answer from
a field that is absent. When it is non-empty, this refund **may** be one this
app posted and may be somebody else's, and nothing on this side can tell —
§5a has the whole story.

It is deliberately **not in `MUST_CONSUME`**, and the two halves of that matter
to you:

- **You are not obliged to read it.** Adding a field to `MUST_CONSUME` makes
  every existing observer `unacknowledged` until it claims the new field, which
  is reserved for a version bump. This one changes nothing about how the report
  is treated.
- **Not declaring the parameter costs nothing.** `frappe.call` drops an
  argument the target does not declare — silently, no `TypeError` — so an
  observer written before this field existed never receives it and behaves
  exactly as it does today, which is to record the refund. That silent drop is
  a hazard for a *required* field (§3) and is precisely what makes an
  *optional* one safe to add.

What it is **for**, if you do read it: reconciling instead of duplicating. You
hold those `Refund Request` rows; we do not know what became of them. A refund
arriving beside one may be that write-back coming back to you.

Three of the required facts are worth stating rather than inferring:

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
  "provider": "shopify", "report_contract_version": 2,
  "outcome":   "reported" | "unacknowledged" | "no_observer" | "own_writeback"
             | "refused" | "failed",
  "delivered": bool,     # true for exactly one outcome: "reported"
  "message":   "<human sentence>",
  "unconsumed": ["settlement_channel"],   # what the observer did not claim
  "recorded_as": "REF-00301",
  "refund_request": "REF-00207",          # set only by the skip, "" else
  "unconfirmed_writeback_on_order": [],   # the same optional list as section 4
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
| `own_writeback` | this app raised the refund itself and read Shopify's answer, so it is not reported to the app that raised it. Normal, and deliberately **not** logged as an error. **The only skip.** See §5a |
| `refused` | the observer declined, or two are registered |
| `failed` | the observer raised. Logged with its traceback |

`unacknowledged` is the state that matters, and it is the safe direction for
absent, unknown and not-yet-implemented — the same principle as
`caller_must_pay` pointing the other way.

---

## 5a. Delivery is at-least-once, and one kind of refund is never delivered

Two properties of this seam that were true and unsaid until now. Both are
stated here because an observer author cannot infer either one from the facts
dict.

### The refund this app raised itself is never reported

`refund.py` writes ERPNext refunds back to Shopify. A successful `refundCreate`
**pays the customer**, and Shopify then fires `refunds/create` for our own
write. Reporting one of those would hand you a refund **you raised and are
already booking**: the second recorder for one refund, which is the two Payment
Entries §0 exists to prevent.

Recognising one takes the **GID**, and the GID exists only on one side of the
post: what is on the Refund Request changes across it, and only the second half
of it names the refund.

```
_set_state(Unverified)     committed BEFORE the post; the GID field is EMPTY
execute(refundCreate)      the post
_set_state(refund_gid)     the GID, and only from a response we READ
```

Version 1 of this document claimed the GID was written *ahead of* the post, so
that the returning webhook could always be recognised as ours. That was
**false**, and it mattered: only the Unverified marker is committed ahead of the
post, and the GID lands after the response has been read. The correction is the
two windows below — and they do **not** both produce a skip.

| window | what the row looks like | lookup | what happens |
|---|---|---|---|
| response read | `shopify_refund_gid` set, status `Done` | `refund.refund_request_for_shopify_refund(refund_id)` — bare id and GID both | `own_writeback`. Not reported, silent |
| posted, never confirmed | GID **empty**, status `Unverified` | `refund.unverified_writebacks_for_order(order_id)` | **reported**, carrying `unconfirmed_writeback_on_order`. Logged |

The first window is certain: the GID is there because we read Shopify's answer
for that exact refund, so ERPNext demonstrably has it and there is nobody to
tell. That one is skipped, and the skip is `own_writeback`.

The second window is every `failed_unknown` outcome of the dispatch contract: a
worker killed after the post, a socket timeout, a 5xx, "Shopify accepted the
request but returned no refund object". Each leaves a row that **is** ours,
carrying no GID, beside an order Shopify may genuinely have refunded — so a
`refunds/create` arriving there is either our own refund coming back or a second
one somebody made, and nothing available on this side can tell which.

**That one is reported, and you are handed the fact instead.** The
`unconfirmed_writeback_on_order` list (§4) names the rows, so you can reconcile
against the `Refund Request` you hold rather than book a second Payment Entry.
Withholding was tried and reversed, and the reason is the doctrine at the
bottom of this section: nothing re-drives a withheld report, so a genuinely
external refund on an order that merely happened to carry an unresolved row was
withheld indefinitely — a **permanent omission**, traded against a duplicate
you are obliged to dedupe anyway.

Reporting it is not silent. Each such delivery goes to this app's Error Log —
"Refund Reported With An Unconfirmed Write-Back" — saying that the report went
out, that no money is at risk either way, and what a person can do about the
open question:

1. check whether this refund is the one that `Refund Request <name>` posted, by
   opening the order in Shopify;
2. resolve that row with `resolve_unverified_writeback` **either way** —
   `"paid"` with the refund's GID if it is the same refund, `"not_paid"` if no
   refund of ours is there. Nothing else clears that state.

Once resolved as paid, the row carries a GID and the ordinary `own_writeback`
skip applies to that refund. Once resolved as not paid, the list is empty again
and reports on that order carry nothing.

> **`utils/credit_note.py` takes the OPPOSITE direction on the same evidence,
> and that is deliberate.** The credit-note path withholds while a write-back
> of ours on the order is unconfirmed, because a duplicate Credit Note is a
> real accounting document somebody has to cancel by hand, and because that
> skip is visible where this one would not be: it writes a `Skipped` row with
> the reason onto the Shopify Log a person opens. Here a duplicate is a report
> you dedupe on `shopify_refund_id`, and withholding is a refund nothing
> records. Opposite costs, opposite defaults; do not "fix" one to match the
> other.

The GID check is inside `report_refund`, the one seam the webhook path and the
backfill path both cross, deliberately **not** in `api.py`: a guard on one path
is a guard the other path forgets. The optional fact is added by the two fact
builders, which are the only two ways facts are made, for the same reason.

### Delivery is at-least-once. **The observer must dedupe on `shopify_refund_id`**

This app keeps **no record of what it has already reported**. Therefore:

- a Shopify webhook retry reports the same refund again;
- a second `backfill_now` of the same order reports every refund on it again;
- `reported: 3` means *three reports were handed over*, not *three distinct
  refunds*. The returned `message` says so in words, so that a count read out
  of a log is not mistaken for a refund count.

What *is* deduped is the same refund appearing twice **within one** backfill
response, where the repeat is visible in the data in hand. Those are dropped
and then named — id by id — in the message and in the log, never silently.
`duplicates` in the returned dict counts them.

The skip is counted and named the same way, as `own_writeback` in the returned
dict and in the message: "Read 1 refund(s); 0 reported" with nothing to explain
the nought is unreadable, and `own_writeback` is the expected outcome for every
refund this app writes back. `unconfirmed_writeback` sits beside it and means
the opposite — those reports **were** delivered, and each is named because
somebody may have to check whose refund it was.

**Why no "already reported" ledger, and why one must not be added here.**
Suppressing a report that was needed is the worse failure of the two. A
duplicate is something you can absorb, and must — you hold the `Refund Request`
and the Payment Entry that prove you have seen it. A ledger on this side that
is only usually right — a row written for a report that never landed, a row
surviving a restore, a row for a refund later amended — converts that duplicate
into a **permanent omission**, which nothing can recover: a refund with no
Cashfree row (#6518's NEFT, every Snapmint refund) would then be recorded by
nothing at all. `_surface()` in the code says what the risk on this path
actually is, in those words: not money — nothing here can move any — but that
"ERPNext never records it".

Every delivery is written to the log with its refund id and its `source`
(`webhook` or `backfill`), so a repeat delivery is visible to a person
afterwards rather than being invisible because it was prevented.

**This is version 2 rather than a footnote to version 1**, and the reasoning
that made it one is worth recording, because it was wrong twice over. It ran:
the facts dict and the acknowledgement protocol are unchanged, which is the
bump criterion, and no observer ever sees `own_writeback` — that outcome exists
precisely because the observer is not called. Both sentences are true and
neither answers the question. A slug entered a vocabulary this document calls
stable and safe to branch on, and the dedupe rule above is a **new obligation**
on the observer. A version number exists so the other side can ask "has
anything I rely on changed?" and get a straight answer; a criterion that only
tracks field shapes cannot give one.

**And it is still version 2 after the reversal above**, by the same criterion
read the same way. The question the number answers is "has anything I rely on
changed since the version I integrated against?", and version 1 is the only
answer point that exists — 2 was drafted and revised in one uncommitted
working tree and never released. Both statements in the header are still
exactly true of the difference from 1. What changed within 2 was the withdrawal
of a slug no released build ever emitted, plus one optional fact whose whole
safety case is that an observer which ignores it is unaffected. Neither is a
thing to re-integrate for, and a bump that says "re-check your integration"
when nothing needs re-checking spends the same credibility a missed bump does.

---

## 6. Entry points

| function | whitelisted | what it does |
|---|---|---|
| `report_refund_from_webhook(payload, shop_domain, shopify_order_name)` | no | the `refunds/create` path. Called by `api.py` |
| `backfill_order_refunds(shopify_order_id, shop_domain)` | no | reads `order.refunds` from Shopify and reports each. Safe to re-run, and expected to be: at-least-once, §5a |
| `backfill_now(shopify_order_id, shop_domain)` | **yes** | the HTTP door for the backfill, gated on Shopify Settings **write** |
| `report_refund(facts)` | no | the seam itself, and where the own-write-back guard lives — §5a |

`backfill_now` is gated on a permission on **this app's own doctype**,
deliberately not on a role list borrowed from `payment_portals` — the same
reasoning that removed the permission check from `write_back_refund` in dispatch
contract version 3. It sends nothing to Shopify and cannot pay anyone, but it
does hand facts to another app that may record a refund from them.

## 7. What this side will never do

- never create a **Payment Entry**, Journal Entry or any other ledger document
  — pinned by a test that inspects every insert and every write;
- never write to **Refund Request**, including this app's own five write-back
  fields — a report is not a write-back. The two lookups in §5a *read* that
  document (`shopify_refund_gid`, and `shopify_writeback_status` for the
  order's rows); neither writes it, and clearing an `Unverified` row is a
  person's job through `resolve_unverified_writeback`;
- never post a **mutation** on the report or backfill path, so no path here can
  move money — pinned by a test asserting the backfill query contains no
  `mutation`;
- never gate reporting on **`enable_refund_writeback`**. That toggle guards the
  payout, and this side must not depend on it whatever it is set to; gating the
  report on it would mean the one safe thing only ran while the dangerous thing
  was armed — this clause used to state the toggle's live value as well, the
  value moved and the clause did not, so no setting for it is recorded here:
  read it off Shopify Settings for the store;
- never keep an **"already reported" ledger** of its own and suppress a report
  from it — see §5a. Delivery is at-least-once and deduping is the observer's
  obligation, because a duplicate is absorbable and an omission is not;
- never **withhold** a report because a write-back of ours on the order is
  unconfirmed. That was tried within version 2 and reversed for the same
  reason as the line above: nothing re-drives a withheld report, so it becomes
  the permanent omission. The suspicion travels as
  `unconfirmed_writeback_on_order` instead. The *credit-note* path does
  withhold on that evidence, on purpose — see the box in §5a;
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
   rather than write to one of yours. Note what such a doctype would **not**
   be for: it must not become the "already reported" ledger §5a rules out, or
   the same row that answers "which refunds were missed" starts causing
   misses.
