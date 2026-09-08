"""
refund_report.py — Report a refund that happened in Shopify to ERPNext.

The opposite direction from `refund.py`.  That module tells Shopify about a
refund raised in ERPNext, and a successful call **pays a customer**.  This one
moves no money at all: the refund has already happened in Shopify, and all that
is left is to make sure ERPNext hears about it.

Report only.  This module never builds a ledger document
-------------------------------------------------------
`payment_portals` is the only app permitted to create a Payment Entry, asserted
there by its own tests, and it already owns the refund ceiling, the deductions,
the accounts and the approval.  It also already **detects the Cashfree refund on
its own** — verified on production, where all four Shopify-originated refunds
arrived as Cashfree refund rows and were linked to the right Sales Order with no
Shopify involvement whatsoever.

So this module is deliberately not a second recorder.  Two triggers for one
refund is two Payment Entries.  What it does is hand over the facts that
`payment_portals` **cannot see**, and stop there:

1. **Refunds that never reach Cashfree.**  #6518's ₹12,999 went out by NEFT.
   Snapmint refunds are not Cashfree refunds, so no Gateway Transaction row will
   ever appear for them and the reconciliation that works for the others cannot
   fire.  Shopify is the only source that knows the refund happened at all.
2. **The Shopify refund's own facts** — refund id and gid, note, line items,
   restock, and which staff user made it.  None of it is derivable from a
   Cashfree row.

The conclusion this module refuses to draw
------------------------------------------
It never says whether a Cashfree refund followed.  It cannot, and the attempt
has produced a wrong answer twice already.

Cashfree-OCC-Notdrones creates these orders and marks them paid *manually*, so
Shopify holds no gateway transaction of its own and logs the refund against the
`manual` gateway.  #6491's timeline reads "manually marked ₹46,952.16 as
refunded" — and that landed as Cashfree refund 144073385 **in the same minute**.
#6518's ₹12,999, same `manual` gateway, went out by NEFT and produced no
Cashfree row ever.  Identical Shopify evidence, opposite settlement.

The discriminator is not in any Shopify payload.  It is `Refund Request
.portal_account -> provider`, which lives in `payment_portals` and is invisible
here.  So `settlement_channel` is always `"undetermined"`, the raw `gateways`
list is copied verbatim, and the decision stays with the app that can make it.
Never read `manual`, or a timeline reading "manually marked as refunded", as
evidence that no money moved.

The seam, and why it needs a positive acknowledgement
-----------------------------------------------------
Coupling stays exactly where REFUND-DISPATCH-CONTRACT.md §1 puts it: this app
never imports `payment_portals`, `payment_portals` never imports this app, and
the call goes through Frappe's hook fan-out.  A consumer registers:

    # payment_portals/hooks.py
    shopify_refund_observers = ["payment_portals.<...>.observe_shopify_refund"]

and is called as ``frappe.call(observer, **facts)``.

`frappe.call` filters the caller's kwargs to the parameters the resolved target
declares.  **An argument the target does not accept is dropped silently** — no
`TypeError`, no warning.  That was proved by accident on `electrobotictest`
once already, and §1 of the dispatch contract turns it into a rule: an argument
that changes a safety decision must be paired with a positive acknowledgement
emitted only on a path that honoured it.

Here, *every* fact in `MUST_CONSUME` changes a decision — an observer that never
received `settlement_channel` will assume the ordinary Cashfree path and leave a
NEFT refund permanently unbooked, which is the exact backlog this feature exists
to fix.  So the acknowledgement is not a bool and not a version number (a
version says what the document promises, not what this call's target accepted).
The observer must **enumerate the fields it consumed**:

    return {"shopify_refund_report_accepted": True,
            "consumed": [...],            # field names it actually used
            "recorded_as": "REF-00301"}   # optional, for the log line

Anything less lands as `unacknowledged`, which means *nobody has been told* and
a person has to look.  That is the safe direction, and it is the default for
absent, unknown and not-yet-implemented — the same principle as
`caller_must_pay` in the other direction.

Refunds this app raised itself are not reported
-----------------------------------------------
`refund.py` writes ERPNext refunds back to Shopify, and Shopify then sends us
`refunds/create` for our own write.  Reporting one of those hands the observer a
refund it raised and is already booking: the second recorder for one refund,
which is the two Payment Entries described above.

Only ONE state proves that, and the state on the Refund Request changes across
the post — only the second half of it names the refund:

    _set_state(Unverified)   committed BEFORE the post; the GID field is EMPTY
    execute(refundCreate)    the post
    _set_state(refund_gid)   the GID, and only from a response we READ

* `refund.refund_request_for_shopify_refund(id)` matches `shopify_refund_gid`,
  so it recognises exactly the refunds whose response we read.  That is the
  certain case: the refund is already recorded, there is nobody to tell, and it
  returns `own_writeback` and is skipped.
* The window in between is a **suspicion, not a skip.**  Every `failed_unknown`
  outcome — a worker killed after the post, a timeout, a 5xx, "Shopify accepted
  the request but returned no refund object" — leaves a row that IS ours,
  carrying no GID, beside an order Shopify may genuinely have refunded.  A
  refund arriving there is either that one coming back or a second one somebody
  made, and nothing here can tell which.

An earlier version of this docstring claimed the GID was written *ahead of* the
post, so that the returning webhook could always be recognised as ours.  It
never was: only the Unverified marker goes in ahead of the post, and the GID
lands after the response.  That sentence asserted a guarantee the guard did not
have, and the window it concealed is where a webhook for our own possibly-paid
refund was reported to `payment_portals` as an externally-made one.

What that suspicion does here: report anyway, and hand over the fact
--------------------------------------------------------------------
The report **goes out**, carrying an optional fact —
`unconfirmed_writeback_on_order`, the `Refund Request` names from
`refund.unverified_writebacks_for_order` — so the observer can reconcile
against its own row instead of duplicating.

Withholding was tried, and it was the wrong direction, because it contradicted
this module's own doctrine two paragraphs down: a genuinely external refund on
an order that happens to carry an unresolved row was withheld indefinitely,
traced by one Error Log line that nothing re-drives, while the harm avoided was
a duplicate the observer is contractually obliged to dedupe anyway.  An
omission is the failure nothing can recover.  Delivering is not silent either
— `_surface` logs the delivery with the row to resolve, because it is the one
delivery a person may need to reconcile by hand.

`utils/credit_note.py` faces the same evidence and **withholds**, on purpose.
A duplicate Credit Note is a real accounting document somebody has to cancel by
hand, and that path's skip is already visible on the Shopify Log row with a
reason, which nothing on this path is.  Opposite costs, opposite directions:
see the comment there before making the two agree.

The GID check lives in `report_refund` — the one seam the webhook and the
backfill both cross — and deliberately **not** in `api.py`, so neither path can
be guarded while the other is forgotten.  The fact is added by the two fact
builders, which are the only two ways facts are made, for the same reason.

Delivery is at-least-once, and the observer must dedupe
------------------------------------------------------
This app keeps **no record of what it has already reported**.  The same refund
is therefore reported again by a Shopify webhook retry, and again by a second
backfill of the same order.  The observer **must** dedupe on
`shopify_refund_id`, which is exactly why that field is in `MUST_CONSUME`.
Duplicate nodes within one backfill run *are* dropped, because there the repeat
is visible in one response — see `backfill_order_refunds`.

That limit is deliberate rather than unfinished.  An "already reported" ledger
that is only usually right converts a duplicate — which the observer can
absorb, and must — into a permanent omission, which nothing can.  A refund with
no Cashfree row (#6518's NEFT, every Snapmint refund) suppressed by a stale
ledger row would be recorded by nothing at all, and `_surface`'s own text says
the risk being managed here is precisely "ERPNext never records it", not money.
Report again; let the observer dedupe.  Every delivery is logged with its
refund id and source, so a repeat is visible to a person afterwards.

Layout follows `refund.py`: pure decision functions first, testable with no
bench, then the frappe-bound orchestration.
"""

import frappe
from frappe.utils import flt

from shopify_integration.utils.shopify_graphql import execute, gid

# Bumped when the shape of the facts dict or the acknowledgement protocol
# changes in a way an observer has to care about.  Adding an optional fact is
# not a bump; adding one to MUST_CONSUME is, because it makes every existing
# observer unacknowledged until it claims the new field.
#
# 2: two things the document calls safe to branch on changed against version 1,
#    and neither is covered by the facts-and-acknowledgement criterion above —
#    which is why "no bump needed, the facts dict is unchanged" was the wrong
#    reading of it.
#      * the OUTCOMES vocabulary gained `own_writeback`, for a refund this app
#        raised and read Shopify's answer for.  A consumer that enumerates
#        outcomes — the document invites exactly that — sees a string version 1
#        never listed.
#      * a new OBLIGATION was placed on the observer: delivery is at-least-once
#        and it must dedupe on `shopify_refund_id`.  That was true and unsaid
#        under version 1, so an observer built against it may not do it.  An
#        obligation is part of the contract even when no field moved.
#    Nothing was removed and no fact changed shape, so an observer that
#    acknowledges the MUST_CONSUME fields and dedupes needs no code change.
#
#    Still 2 after round 3, deliberately, and version 1 is the reason: both
#    statements above are still exactly true of the difference from 1, which is
#    the only version any consumer can have integrated against — nothing here
#    is committed and 2 has never been released.  Round 3 withdrew a second
#    slug this draft of 2 had carried (`own_writeback` + `_unverified`, for a
#    withheld report), and added `unconfirmed_writeback_on_order` as an
#    OPTIONAL fact.  Neither adds a third thing to re-integrate for: the
#    withdrawal shrinks the vocabulary back toward 1 on a slug no released
#    build ever emitted, and the optional fact is dropped by frappe.call for an
#    observer that does not declare it, which then behaves exactly as it does
#    today.  Bumping to 3 would announce a re-integration to a consumer who has
#    never seen 2.
REPORT_CONTRACT_VERSION = 2

# The hook an observer registers under.  Named for what it does — observe a
# refund that already happened — rather than for who consumes it, so a second
# app could read it without the name lying.  Nothing is registered by this app.
OBSERVER_HOOK = "shopify_refund_observers"

PROVIDER = "shopify"

# ── Outcome vocabulary ───────────────────────────────────────────────────────
# Stable slugs, safe to branch on.  `message` is for humans and may be reworded.

# The observer took the report and enumerated what it consumed.
OUTCOME_REPORTED = "reported"
# It answered, but did not claim every fact a correct decision needs — either
# because frappe.call dropped the field, or because it stayed silent about it.
# Nobody has been told.  A person must look.
OUTCOME_UNACKNOWLEDGED = "unacknowledged"
# No observer is registered.  Expected on a site without payment_portals, and
# deliberately not an error.
OUTCOME_NO_OBSERVER = "no_observer"
# This app raised the refund itself: refund.py posted refundCreate, read the
# response, stored the GID, and Shopify sent the resulting refunds/create
# straight back to us.  The app that would record it is the app that raised it,
# so there is nobody to tell.  A deliberate skip, not a fault, and not logged
# as one — ERPNext demonstrably has this refund.
OUTCOME_OWN_WRITEBACK = "own_writeback"
# The observer declined, or the registration is a misconfiguration.
OUTCOME_REFUSED = "refused"
# The observer raised.
OUTCOME_FAILED = "failed"

OUTCOMES = (
    OUTCOME_REPORTED,
    OUTCOME_UNACKNOWLEDGED,
    OUTCOME_NO_OBSERVER,
    OUTCOME_OWN_WRITEBACK,
    OUTCOME_REFUSED,
    OUTCOME_FAILED,
)

# The outcomes where NOT reporting was the decision rather than the failure.
# There is exactly one, and it is the certain one: ERPNext already has that
# refund.  A second slug lived here for a report withheld on the ORDER-scoped
# suspicion; it is gone, and the set is kept as a set rather than folded into a
# comparison so that any future skip has to be added here — where
# `needs_report_note` and `_surface` both read it — rather than in one of them.
DELIBERATE_SKIPS = frozenset({
    OUTCOME_OWN_WRITEBACK,
})

# What this app will not conclude.  See the module docstring.
SETTLEMENT_UNDETERMINED = "undetermined"

ACK_KEY = "shopify_refund_report_accepted"
CONSUMED_KEY = "consumed"

# Facts whose silent loss changes a decision on the other side.  Every one of
# these must appear in the observer's `consumed` list for the report to count as
# delivered.
#
#   shopify_refund_id   identity; without it a second webhook is a second
#                       refund.  Delivery is at-least-once, so deduping on
#                       this field is the observer's obligation, not an
#                       optimisation — see the module docstring
#   shopify_order_id    which order — the payload's own "id" is NOT this
#   amount              how much; there is no total field, it is summed here
#   gateways            the raw gateway, the only settlement evidence that exists
#   settlement_channel  the explicit "I do not know" that stops the misreading
MUST_CONSUME = frozenset({
    "shopify_refund_id",
    "shopify_order_id",
    "amount",
    "gateways",
    "settlement_channel",
})


def delivered(outcome: str) -> bool:
    """True for exactly one outcome.

    Stated as a function rather than left to the caller for the same reason
    `retry_safe` is stated in the dispatch contract: this is the axis where
    getting the mapping wrong means a refund silently goes unrecorded, and a
    boolean is harder to get wrong than a string compare against a remembered
    set.
    """
    return outcome == OUTCOME_REPORTED


def needs_report_note(outcome: str) -> bool:
    """Whether the Shopify Log row for this webhook should carry the report's
    message.

    Stated here rather than left to `api.py` for the reason `delivered` is
    stated: the caller was branching on `delivered` alone, which is right for a
    genuine non-delivery and wrong for the deliberate skip.  `own_writeback` is
    the EXPECTED outcome for every refund this app writes back, so that pasted
    its explanatory sentence into the `error_message` of every one of those
    webhooks — and an error field carrying a routine sentence is an error field
    people stop reading.

    A delivered report writes nothing here either, including the one delivered
    beside an unconfirmed write-back of ours: the note exists for a report that
    did NOT land, and that delivery's reconciliation line is already in the
    Error Log with what to do.  The Shopify Log row is not a second copy of it.
    """
    return not (delivered(outcome) or outcome in DELIBERATE_SKIPS)


# ── Pure fact extraction ─────────────────────────────────────────────────────

def _successful_refund_transactions(transactions) -> list:
    """The refund transactions that actually moved money.

    `kind` must be a refund and `status` a success.  A failed refund
    transaction moved nothing and counting it reports a larger refund than
    happened; a `sale` row on the same payload is the original capture.
    """
    out = []
    for txn in transactions or []:
        if not isinstance(txn, dict):
            continue
        if str(txn.get("kind", "")).lower() != "refund":
            continue
        if str(txn.get("status", "")).lower() != "success":
            continue
        out.append(txn)
    return out


def _gateways(transactions) -> list:
    """Gateway names, verbatim and deduplicated, order preserved.

    Copied rather than interpreted.  See the module docstring: `manual` is not
    evidence of anything, but it is the only evidence there is, so it is handed
    over exactly as Shopify wrote it.
    """
    seen, out = set(), []
    for txn in transactions:
        name = (txn.get("gateway") or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _line_items(refund_line_items) -> list:
    """The refunded lines, flattened to what a reader needs."""
    out = []
    for row in refund_line_items or []:
        if not isinstance(row, dict):
            continue
        item = row.get("line_item") or {}
        out.append({
            "line_item_id": str(row.get("line_item_id") or item.get("id") or ""),
            "sku": item.get("sku") or "",
            "title": item.get("title") or "",
            "quantity": int(row.get("quantity") or 0),
            "restock_type": row.get("restock_type") or "",
            "subtotal": flt(row.get("subtotal")),
            "total_tax": flt(row.get("total_tax")),
        })
    return out


def _restocked(line_items) -> bool:
    """Whether Shopify put anything back into stock.

    Derived from the lines rather than read off the refund's top-level
    `restock`, which is an input flag and not reliably echoed.  ERPNext is the
    inventory master, so this is reported for reconciliation rather than acted
    on.
    """
    return any(
        line["restock_type"] not in ("", "no_restock")
        for line in line_items
    )


def facts_from_webhook(payload: dict, shop_domain: str = "",
                       shopify_order_name: str = "") -> dict:
    """The facts of a Shopify refund, from a `refunds/create` REST payload.

    Writes nothing and calls nothing over the network.  It is no longer *pure*:
    `unconfirmed_writeback_on_order` needs one read of this app's own write-back
    state, and it belongs to the facts rather than to the seam because it is a
    fact — something the observer is told, exactly like `gateways`.  It sits
    here rather than in `report_refund` so that a caller that builds facts and
    inspects them sees the same dict the observer will, and so that both
    entry points carry it: `facts_from_graphql` routes through this function.
    The read never raises; see `_unconfirmed_writebacks_on_order`.

    The one trap worth restating, because api.py already carries a comment
    about it for the log row: the payload's own ``id`` is the **refund** id.
    ``order_id`` is the order.  Conflating them files a refund against the
    wrong order.
    """
    payload = payload or {}
    txns = _successful_refund_transactions(payload.get("transactions"))
    lines = _line_items(payload.get("refund_line_items"))
    refund_id = str(payload.get("id") or "")
    order_id = str(payload.get("order_id") or "")

    amount = 0.0
    for txn in txns:
        amount += flt(txn.get("amount"))

    currencies = [c for c in
                  {(t.get("currency") or "").strip() for t in txns} if c]

    return {
        "report_contract_version": REPORT_CONTRACT_VERSION,
        "provider": PROVIDER,
        "source": "webhook",

        # Identity.  Two different numbers; see above.
        "shopify_refund_id": refund_id,
        "shopify_refund_gid": gid("Refund", refund_id) if refund_id else "",
        "shopify_order_id": order_id,
        "shopify_order_name": shopify_order_name or "",
        "shopify_store": shop_domain or "",

        # OPTIONAL, and deliberately NOT in MUST_CONSUME.  The Refund Requests
        # of ours on this order whose write-back was posted and never
        # confirmed — so this refund MAY be one of them, and may be somebody
        # else's, and nothing here can tell.  Handed over instead of
        # withholding the report: the observer holds those rows and can
        # reconcile, while a withheld report is an omission nothing recovers.
        #
        # Optional because a mandatory fact makes every existing observer
        # unacknowledged until it claims the field, which is a version bump.
        # An observer that does not declare the parameter has it dropped by
        # frappe.call and behaves exactly as it does today — which is to
        # deliver — and that is the safe direction for a fact nobody is
        # obliged to read yet.  Always a list, so "none" is not the same
        # answer as "this build does not report them".
        "unconfirmed_writeback_on_order":
            _unconfirmed_writebacks_on_order(order_id),

        # Money.  There is no total on a refund payload — it is the sum of the
        # successful refund transactions, which is also why a split refund adds
        # up correctly and a failed one does not inflate the figure.
        "amount": amount,
        "currency": currencies[0] if len(currencies) == 1 else "",
        "transaction_count": len(txns),
        "gateways": _gateways(txns),

        # The conclusion this app refuses to draw.  Always this value.
        "settlement_channel": SETTLEMENT_UNDETERMINED,

        # The refund's own facts, which no Cashfree row carries.
        "note": payload.get("note") or "",
        "shopify_user_id": str(payload.get("user_id") or ""),
        "created_at": payload.get("created_at") or "",
        "processed_at": payload.get("processed_at") or "",
        "line_items": lines,
        "restocked": _restocked(lines),
        # Shopify does not persist `notify` on the refund resource — it is
        # write-only on the input.  Reporting False would invent a fact, and a
        # reader would take it as "the customer was not emailed".
        "notify": None,
    }


def facts_from_graphql(refund_node: dict, shopify_order_id: str = "",
                       shop_domain: str = "",
                       shopify_order_name: str = "") -> dict:
    """The same facts, from a GraphQL `order.refunds` node, for the backfill.

    Deliberately routed through the same shape as `facts_from_webhook` so an
    observer cannot tell a backfilled refund from a live one except by `source`
    — a report that behaved differently depending on how it was discovered
    would be two seams wearing one name.

    `unconfirmed_writeback_on_order` comes with that, from the `order_id` put
    into the payload below.  Routed rather than repeated on purpose: a second
    copy of the lookup here is a second place for the two paths to disagree,
    which is the whole failure mode the shared builder exists to prevent.
    """
    refund_node = refund_node or {}
    node_gid = refund_node.get("id") or ""
    numeric = node_gid.rsplit("/", 1)[-1] if node_gid else ""

    txns = []
    for node in _transaction_nodes(refund_node.get("transactions")):
        money = ((node.get("amountSet") or {}).get("presentmentMoney") or {})
        txns.append({
            "kind": node.get("kind") or "REFUND",
            "status": node.get("status") or "SUCCESS",
            "gateway": node.get("gateway") or "",
            "amount": money.get("amount"),
            "currency": money.get("currencyCode") or "",
        })

    payload = {
        "id": numeric,
        "order_id": shopify_order_id,
        "note": refund_node.get("note") or "",
        "created_at": refund_node.get("createdAt") or "",
        "processed_at": refund_node.get("createdAt") or "",
        "transactions": txns,
        "refund_line_items": [],
    }
    facts = facts_from_webhook(payload, shop_domain=shop_domain,
                               shopify_order_name=shopify_order_name)
    facts["source"] = "backfill"
    # The node's own gid is authoritative; do not rebuild it from a split.
    if node_gid:
        facts["shopify_refund_gid"] = node_gid

    total = ((refund_node.get("totalRefundedSet") or {})
             .get("presentmentMoney") or {})
    if not facts["amount"] and total.get("amount"):
        # A refund whose transactions the query did not return still has a
        # total.  Better a right figure from a second field than a zero that
        # reads as "no money moved".
        facts["amount"] = flt(total.get("amount"))
    return facts


def _transaction_nodes(container) -> list:
    """list, {"nodes": [...]} or {"edges": [{"node": ...}]}.

    Same shape asymmetry `refund.transaction_nodes` exists for, and the same
    reason to tolerate all three: guessing wrong fails silently as an empty
    list, which here would read as "no money moved".
    """
    if not container:
        return []
    if isinstance(container, list):
        return [n for n in container if isinstance(n, dict)]
    if isinstance(container, dict):
        if isinstance(container.get("nodes"), list):
            return [n for n in container["nodes"] if isinstance(n, dict)]
        if isinstance(container.get("edges"), list):
            return [
                edge["node"] for edge in container["edges"]
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
            ]
    return []


# ── The result dict ──────────────────────────────────────────────────────────

def _result(outcome: str, message: str, facts: dict,
            unconsumed=(), recorded_as: str = "", observer: str = "",
            refund_request: str = "") -> dict:
    """Every path returns this shape.  `report_refund` never raises.

    `refund_request` is set only by the skip, and it names the row the decision
    was made against — so the backfill can list what it did not report without
    re-deriving the name out of a prose message.

    `unconfirmed_writeback_on_order` is copied out of the facts for the same
    reason: `_surface` and the backfill both have to know a delivered report
    may need reconciling, and re-reading the database to find out would be a
    second answer to a question already answered.
    """
    return {
        "provider": PROVIDER,
        "report_contract_version": REPORT_CONTRACT_VERSION,
        "outcome": outcome,
        # "" on every path that is not the skip: no Refund Request was involved.
        "refund_request": refund_request,
        "unconfirmed_writeback_on_order": list(
            (facts or {}).get("unconfirmed_writeback_on_order") or []),
        # One boolean instead of a remembered set of strings.  See `delivered`.
        "delivered": delivered(outcome),
        "message": message,
        "unconsumed": sorted(unconsumed),
        "recorded_as": recorded_as,
        "observer": observer,
        "shopify_refund_id": (facts or {}).get("shopify_refund_id", ""),
        "shopify_order_id": (facts or {}).get("shopify_order_id", ""),
        "amount": (facts or {}).get("amount", 0.0),
        "settlement_channel": (facts or {}).get("settlement_channel",
                                                SETTLEMENT_UNDETERMINED),
    }


def _consumed_fields(answer) -> set:
    """What the observer claims it used, defensively.

    A non-list, a list of non-strings, or a missing key all mean "claimed
    nothing" rather than "claimed everything".  An observer cannot acquire an
    acknowledgement by returning something malformed.
    """
    if not isinstance(answer, dict):
        return set()
    claimed = answer.get(CONSUMED_KEY)
    if isinstance(claimed, (list, tuple, set, frozenset)):
        return {str(f) for f in claimed}
    return set()


# ── The own-write-back guard ─────────────────────────────────────────────────

def _own_writeback(shopify_refund_id) -> str:
    """The Refund Request this refund was written back from, or "".

    Lazily imported, mirroring the same guard on the credit-note path
    (`utils/credit_note.py`): pulling `refund` — the module that can pay a
    customer — into the import graph of every webhook, for a check nearly every
    payload answers with "not ours", is a coupling worth not having.  That
    function matches both the bare numeric id and the GID, so it is handed the
    id as the payload gave it rather than a form guessed here.

    A guard that cannot *decide* answers "" — report it — and logs why.  The two
    ways of being wrong are not symmetrical: a report the observer has already
    seen is a duplicate it dedupes on `shopify_refund_id`, and it holds the
    Refund Request that would tell it so.  A report suppressed by a broken guard
    is a refund nothing records, ever.
    """
    try:
        from shopify_integration.utils.refund import (
            refund_request_for_shopify_refund,
        )
        return refund_request_for_shopify_refund(shopify_refund_id) or ""
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Shopify: Refund Own-Write-Back Check Failed — {}".format(
                shopify_refund_id or ""),
        )
        return ""


def _unconfirmed_writebacks_on_order(shopify_order_id) -> list:
    """Every unresolved write-back of ours on this order, or [].

    Not a guard.  This is the window `_own_writeback` cannot see —
    `shopify_refund_gid` is written only from a refundCreate response we read,
    so a write-back that lost its answer leaves a row that is ours with that
    field empty, and the order is the only handle on it — and what comes back
    is a *suspicion*, which this module reports rather than acts on.  It
    becomes the `unconfirmed_writeback_on_order` fact, so the observer can
    reconcile against its own row instead of booking a second Payment Entry.

    EVERY match, not one of them: an order can carry two unresolved rows, and
    naming one of two tells a person the order is clear once that one is
    cleared.  `unverified_writebacks_for_order` sorts them and filters to
    submitted rows, and never raises — see its docstring for both reasons.

    Lazily imported for the same reason as `_own_writeback`: pulling in the
    module that can pay a customer, for a lookup nearly every payload answers
    with "nothing", is a coupling worth not having.  [] when it cannot decide,
    which errs the same way everything else here does — an absent fact leaves
    the observer where it is today, and today it delivers.
    """
    try:
        from shopify_integration.utils.refund import (
            unverified_writebacks_for_order,
        )
        return list(unverified_writebacks_for_order(shopify_order_id) or [])
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Shopify: Refund Unconfirmed-Write-Back Lookup Failed — {}".format(
                shopify_order_id or ""),
        )
        return []


# ── The seam ─────────────────────────────────────────────────────────────────

def report_refund(facts: dict) -> dict:
    """Hand one Shopify refund's facts to whichever app registered to hear it.

    Never raises.  This runs inside a webhook that must return 200, about a
    refund that has *already happened* in Shopify — failing loudly would make
    Shopify retry an event that is not the problem, and no retry can un-refund
    anything.

    Writes no ledger document, and nothing at all to Refund Request: reporting
    is the whole of the job.  See §8 of REFUND-DISPATCH-CONTRACT.md for the
    list of what this app will never touch.

    Two properties of this seam that callers have to know, because both were
    true and unsaid once:

    * **A refund this app wrote back is not reported** — the one skip, and it
      needs the GID, which means Shopify's answer was read.  The guard is here,
      not in `api.py`, because this function is the one point the webhook path
      and the backfill path both pass through.  A refund on an order whose
      write-back is merely *unresolved* IS reported, carrying
      `unconfirmed_writeback_on_order`; see the module docstring for why that
      direction and not the other.
    * **Delivery is at-least-once.**  Nothing here remembers what it has
      already reported, so a webhook retry or a second backfill reports the
      same refund again.  The observer must dedupe on `shopify_refund_id`.
      That is deliberate: suppressing a report that was needed is the worse
      failure of the two, and the module docstring says why.
    """
    facts = facts or {}

    # ── The loop guard: the GID, and nothing else ───────────────────────────
    # Checked before the observers lookup, and before anything is built,
    # because there is nothing to do at all in this case: our own refundCreate
    # fired the refunds/create that brought us here, and the app registered to
    # record refunds is the app that raised this one.  Telling it would be the
    # second recorder for one refund.
    #
    # This is the whole of the skip.  The GID exists only because refund.py
    # READ Shopify's answer for this very refund, so the refund is certainly
    # ours and certainly already recorded — and a refund we hold the GID for
    # must stay certain even when an unresolved write-back sits elsewhere on
    # the same order, which is why this runs before the order-scoped lookup and
    # why that lookup no longer decides anything.
    own_refund = _own_writeback(facts.get("shopify_refund_id"))
    if own_refund:
        frappe.logger().info(
            "Shopify: refund {refund} was written back from Refund Request "
            "{name} — not reported; the app that raised it is already "
            "recording it.".format(
                refund=facts.get("shopify_refund_id", ""), name=own_refund)
        )
        return _result(
            OUTCOME_OWN_WRITEBACK,
            "Shopify refund {refund} was written back from Refund Request "
            "{name}, so it was not reported: ERPNext raised this refund and "
            "the app that raised it is already recording it. Reporting it "
            "would be the second recorder for one refund.".format(
                refund=facts.get("shopify_refund_id", ""), name=own_refund),
            facts,
            refund_request=own_refund,
        )

    # ── ...and no second branch for the window the GID cannot cover ─────────
    # A write-back that lost its answer leaves the Refund Request on Unverified
    # with the GID field empty, so the guard above misses it while Shopify may
    # genuinely hold the refund.  That used to WITHHOLD the report.  It no
    # longer does, and this comment is where the next person will look before
    # putting it back:
    #
    # withholding turned an undecidable case into a permanent omission.  A
    # genuinely external refund on an order that happened to carry an
    # unresolved row was withheld indefinitely — nothing re-drives a withheld
    # report — and for a refund with no Cashfree row (#6518's NEFT, every
    # Snapmint refund) nothing else would ever surface it.  The harm it avoided
    # was a duplicate the observer is contractually obliged to dedupe on
    # shopify_refund_id anyway, holding the Refund Request that proves it.
    # Between those two this module's doctrine is not close, and it is stated
    # in its own docstring and in _surface's text: an omission is the failure
    # nothing can recover.
    #
    # So the suspicion travels as a FACT instead —
    # `unconfirmed_writeback_on_order`, put on the facts by the two fact
    # builders — and _surface logs the delivery with the row to resolve.  The
    # credit-note path chooses the other direction on the same evidence, for
    # reasons the comment in utils/credit_note.py gives; that asymmetry is
    # deliberate.
    observers = list(frappe.get_hooks(OBSERVER_HOOK) or [])

    if not observers:
        # Expected on a site with no payment_portals.  Not an error, but not a
        # success either: the refund's facts have gone nowhere.
        return _result(
            OUTCOME_NO_OBSERVER,
            f"No app is registered under {OBSERVER_HOOK} to receive Shopify "
            f"refunds, so this refund has not been reported to ERPNext.",
            facts,
        )

    if len(observers) > 1:
        # A second reader is not a second payout, but it is a second recorder,
        # and two recorders for one refund is the two Payment Entries this
        # design exists to avoid.  Refuse rather than pick one.
        return _result(
            OUTCOME_REFUSED,
            f"{len(observers)} apps are registered under {OBSERVER_HOOK}; "
            f"expected one. Two recorders for one refund is two Payment "
            f"Entries, so nothing was reported.",
            facts,
        )

    observer = observers[0]

    try:
        answer = frappe.call(observer, **facts)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Refund Report Failed — {facts.get('shopify_refund_id', '')}",
        )
        return _result(
            OUTCOME_FAILED,
            "The app registered to receive Shopify refunds raised an error; "
            "see the Error Log. This refund has not been reported.",
            facts, observer=observer,
        )

    accepted = isinstance(answer, dict) and answer.get(ACK_KEY) is True
    if not accepted:
        message = ""
        if isinstance(answer, dict):
            message = str(answer.get("message") or "")
        return _result(
            OUTCOME_REFUSED if isinstance(answer, dict) and ACK_KEY in answer
            else OUTCOME_UNACKNOWLEDGED,
            message or (
                "The app registered to receive Shopify refunds did not "
                f"acknowledge the report (no {ACK_KEY}), so this refund must "
                "be treated as unreported."
            ),
            facts, observer=observer,
        )

    unconsumed = MUST_CONSUME - _consumed_fields(answer)
    if unconsumed:
        # The acknowledgement is not a bool and not a version number, for the
        # reason §1 of the dispatch contract gives: frappe.call drops kwargs
        # the target does not declare, so an observer built against an older
        # field set answers True having never seen the field. A version says
        # what the document promises, not what this call's target accepted.
        return _result(
            OUTCOME_UNACKNOWLEDGED,
            "The app registered to receive Shopify refunds acknowledged the "
            "report without claiming " + ", ".join(sorted(unconsumed)) +
            ". Those fields decide how the refund is recorded, so it is "
            "treated as unreported — most likely that app predates them.",
            facts, unconsumed=unconsumed, observer=observer,
        )

    recorded_as = str(answer.get("recorded_as") or "")
    # One line per delivery.  Delivery is at-least-once and this app keeps no
    # ledger of what it has reported, so this line is the only way a person
    # answering "was this refund recorded twice?" can see that the same refund
    # was handed over twice.  It names the refund and how it was discovered,
    # because a webhook retry and a backfill of the same order look identical
    # from the observer's side.
    frappe.logger().info(
        "Shopify: reported refund {refund} ({source}) to {observer}{recorded}. "
        "Delivery is at-least-once — this line twice for one refund id is a "
        "redelivery, not a second refund.".format(
            refund=facts.get("shopify_refund_id", ""),
            source=facts.get("source", "") or "unknown source",
            observer=observer,
            recorded=" as " + recorded_as if recorded_as else "",
        )
    )
    return _result(
        OUTCOME_REPORTED,
        "Reported to " + observer + ".",
        facts,
        recorded_as=recorded_as,
        observer=observer,
    )


# ── Frappe-bound: the webhook path ───────────────────────────────────────────

def _settings_for_store(shop_domain: str):
    """Shopify Settings for a store, or None.

    Gated on `enable_sync` only, and deliberately **not** on
    `enable_refund_writeback`.  That toggle guards the payout in `refund.py`,
    and this lookup must not depend on it whatever it is set to: a report moves
    no money, and gating it on the payout switch would mean the one safe thing
    only ran while the dangerous thing was armed.

    This docstring used to name the toggle's live value here as well, and the
    value moved without it — which is the argument for stating the reasoning
    and leaving the setting to be read off Shopify Settings.
    """
    name = frappe.db.get_value(
        "Shopify Settings", {"shop_domain": shop_domain, "enable_sync": 1}, "name"
    )
    return frappe.get_doc("Shopify Settings", name) if name else None


def _surface(result: dict, facts: dict) -> None:
    """Make a report that did not land impossible to miss.

    `unacknowledged` is the state that matters: the refund happened, ERPNext has
    not been told, and no retry changes that on its own — the observer needs
    registering or upgrading first.  It goes to the Error Log, the same channel
    `refund.py` uses for an Unverified write-back, because both mean "a person
    must look".

    One DELIVERED report goes there too — the one handed over while a
    write-back of ours on that order was still unconfirmed.  See the first
    branch below.
    """
    unconfirmed = list(result.get("unconfirmed_writeback_on_order") or [])
    if result.get("delivered"):
        if unconfirmed:
            # The one delivery that may need a person, and the reason it is
            # logged is not that the report is doubtful — it landed — but that
            # nobody yet knows whose refund it was.  Reporting was still right:
            # withholding it would have been an omission nothing recovers.  What
            # is left is a reconciliation somebody may have to do by hand, and
            # the two facts that make it possible are here rather than in the
            # Shopify Log row, because the row a person acts on is the Refund
            # Request named below.
            frappe.log_error(
                "Shopify refund {refund} was reported to ERPNext while a "
                "write-back of ours on the same order was still "
                "unconfirmed.\n\n"
                "Shopify refund : {refund}\n"
                "Shopify order  : {order}\n"
                "Amount         : {amount} {currency}\n"
                "Refund Request : {rows}\n\n"
                "No money is at risk from this report either way — the refund "
                "already happened in Shopify, and nothing here can move any. "
                "What is undecided is WHOSE refund it is: the Refund "
                "Request(s) above posted a refund to Shopify for this order "
                "and never learned the outcome, so this may be that refund "
                "coming back to us, or a second one somebody made in "
                "Shopify.\n\n"
                "The report went out on purpose. The app that records refunds "
                "dedupes on the Shopify refund id and holds those rows, so a "
                "repeat is absorbable; a report withheld here would have been "
                "a refund nothing ever records.\n\n"
                "To close it out:\n"
                "  1. check whether this refund is the one that Refund "
                "Request(s) {rows} posted — open the order in Shopify;\n"
                "  2. resolve that row with resolve_unverified_writeback "
                "either way ('paid' with this refund's GID if it is the same "
                "refund, 'not_paid' if no refund of ours is there). Nothing "
                "else clears that state, and while it stands every further "
                "refund on this order is reported with the same "
                "line.".format(
                    refund=facts.get("shopify_refund_id", ""),
                    order=facts.get("shopify_order_id", ""),
                    amount=facts.get("amount", 0),
                    currency=facts.get("currency", ""),
                    rows=", ".join(unconfirmed),
                ),
                "Shopify: Refund Reported With An Unconfirmed Write-Back "
                "— {}".format(facts.get("shopify_refund_id", "")),
            )
        return
    if (result.get("outcome") == OUTCOME_NO_OBSERVER
            or result.get("outcome") in DELIBERATE_SKIPS):
        # A site without payment_portals is a normal configuration, not a fault.
        # Nothing consumes these reports there and nothing waits for one.
        #
        # The deliberate skips are read off DELIBERATE_SKIPS rather than named
        # here, because the set's own comment promises that this function and
        # needs_report_note both consult it — and a literal tuple here made
        # that promise false: a future skip added to the set would have been
        # honoured by the note and written to the Error Log by this, which is
        # the drift the set exists to prevent.  no_observer stays separate; it
        # is a configuration, not a decision not to report.
        #
        # For own_writeback the argument applies with more force than for
        # no_observer: ERPNext raised that refund and already has it, so there
        # is nothing for a person to do — and a line here would appear for
        # every refund this app writes back, which is how an Error Log stops
        # being read.
        return
    gateways = ", ".join(facts.get("gateways") or []) or "(none)"
    unclaimed = ", ".join(result.get("unconsumed") or []) or "(n/a)"
    frappe.log_error(
        "{message}\n\n"
        "Shopify refund : {refund}\n"
        "Shopify order  : {order}\n"
        "Amount         : {amount} {currency}\n"
        "Gateways       : {gateways}\n"
        "Outcome        : {outcome}\n"
        "Not claimed    : {unclaimed}\n"
        # Named on this path too: a report that did not land on an order
        # carrying an unconfirmed write-back of ours is the same
        # reconciliation, and a person reading only this line would not know
        # to do it.
        "Unconfirmed    : {unconfirmed}\n\n"
        "No money is at risk — the refund already happened in Shopify. What is "
        "at risk is that ERPNext never records it, which for a refund with no "
        "Cashfree row (NEFT, Snapmint) means nothing else will ever surface "
        "it.".format(
            message=result.get("message", ""),
            refund=facts.get("shopify_refund_id", ""),
            order=facts.get("shopify_order_id", ""),
            amount=facts.get("amount", 0),
            currency=facts.get("currency", ""),
            gateways=gateways,
            outcome=result.get("outcome", ""),
            unclaimed=unclaimed,
            unconfirmed=", ".join(unconfirmed) or "(none)",
        ),
        "Shopify: Refund Not Reported — {}".format(
            facts.get("shopify_refund_id", "")),
    )


def report_refund_from_webhook(payload: dict, shop_domain: str = "",
                               shopify_order_name: str = "") -> dict:
    """Extract and report one `refunds/create` payload.  Never raises.

    The single line `api.py` calls.  It runs inside a webhook that must return
    200 about a refund that has already happened, so every failure is a result
    dict and a log line rather than an exception — a non-200 makes Shopify retry
    an event that is not the problem, and no retry can un-refund anything.
    """
    try:
        facts = facts_from_webhook(payload, shop_domain=shop_domain,
                                   shopify_order_name=shopify_order_name)
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         "Shopify: Refund Report Extraction Failed")
        return _result(OUTCOME_FAILED,
                       "Could not read the Shopify refund payload; see the "
                       "Error Log. This refund has not been reported.", {})

    result = report_refund(facts)
    _surface(result, facts)
    return result


# ── Frappe-bound: the backfill path ──────────────────────────────────────────

_ORDER_REFUNDS_QUERY = """
query OrderRefunds($orderId: ID!) {
  order(id: $orderId) {
    id
    name
    refunds(first: 20) {
      id
      note
      createdAt
      totalRefundedSet { presentmentMoney { amount currencyCode } }
      transactions(first: 10) {
        edges { node { id kind status gateway
                       amountSet { presentmentMoney { amount currencyCode } } } }
      }
    }
  }
}
"""


def _refund_nodes(container) -> list:
    """`order.refunds` tolerated as list, nodes or edges.

    `Order.refunds` takes `first:` and the docs show it returning a plain list,
    while the same field elsewhere appears as a connection.  Neither shape could
    be verified against a live response, and guessing wrong fails silently as an
    empty list — which here would read as "this order has no refunds" and report
    nothing at all.  Same reasoning as `refund.transaction_nodes`.
    """
    return _transaction_nodes(container)


def _skipped_ids(reports, outcome: str) -> list:
    """"<refund id> (<Refund Request>)" for each report with that outcome.

    Named rather than counted, so whoever reads the backfill's message can go
    and look at the row the decision was made against.  A refund with no id at
    all still gets an entry, so the count and the list can never disagree.
    """
    out = []
    for result in reports:
        if result.get("outcome") != outcome:
            continue
        refund_id = result.get("shopify_refund_id") or "(unnamed refund)"
        against = result.get("refund_request") or ""
        out.append(f"{refund_id} ({against})" if against else refund_id)
    return out


def _reconcile_ids(reports) -> list:
    """"<refund id> (<Refund Request>, ...)" for each DELIVERED report handed
    over while a write-back of ours on that order was unconfirmed.

    Same rule as `_skipped_ids` and the dropped duplicates: whatever a person
    may have to act on is named, not counted.  These were reported — the
    backfill did its job — but each one is a refund whose owner is undecided,
    and an operator reading "Read 2 refund(s); 2 reported" would have no way to
    know that one of them needs checking against a row of ours.
    """
    out = []
    for result in reports:
        if not result.get("delivered"):
            continue
        rows = list(result.get("unconfirmed_writeback_on_order") or [])
        if not rows:
            continue
        refund_id = result.get("shopify_refund_id") or "(unnamed refund)"
        out.append("{} ({})".format(refund_id, ", ".join(rows)))
    return out


def backfill_order_refunds(shopify_order_id: str, shop_domain: str = "") -> dict:
    """Read the refunds Shopify already holds for one order, and report each.

    For the refunds that predate the webhook, and for the ones that will never
    produce a Gateway Transaction row — #6518's NEFT, every Snapmint refund —
    where Shopify is the only source that knows the refund happened at all.

    Reads Shopify and reports.  There is no mutation on this path, so it sends
    nothing to Shopify and cannot move money.

    **Delivery is at-least-once.**  Running this twice reports every refund
    twice: nothing here records what has already been reported, on purpose (the
    module docstring gives the reason — a ledger that is only usually right
    turns a duplicate the observer absorbs into a permanent omission nothing
    can).  The observer dedupes on `shopify_refund_id`.  So `reported` is a
    count of reports handed over, **not** a count of distinct refunds, and the
    returned message says so for whoever reads it out of a log.

    What *is* deduped is the same refund appearing twice within one response,
    because there the repeat is visible.  Whatever gets dropped that way is
    named in the message and in the log; a count with no ids is the REF-00207
    failure, where rows were examined, discarded, and never seen again.
    """
    if not shopify_order_id:
        return {"reports": [], "read": 0, "reported": 0, "duplicates": 0,
                "own_writeback": 0, "unconfirmed_writeback": 0,
                "shopify_order_id": "",
                "message": "No Shopify order id given."}

    settings = _settings_for_store(shop_domain) if shop_domain else None
    if not settings:
        return {"reports": [], "read": 0, "reported": 0, "duplicates": 0,
                "own_writeback": 0, "unconfirmed_writeback": 0,
                "shopify_order_id": shopify_order_id,
                "message": "No enabled Shopify Settings for store {}.".format(
                    shop_domain or "(unspecified)")}

    try:
        data = execute(
            settings,
            _ORDER_REFUNDS_QUERY,
            {"orderId": gid("Order", shopify_order_id)},
            operation="OrderRefunds",
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Refund Backfill Read Failed — {shopify_order_id}")
        return {"reports": [], "read": 0, "reported": 0, "duplicates": 0,
                "own_writeback": 0, "unconfirmed_writeback": 0,
                "shopify_order_id": shopify_order_id,
                "message": "Could not read refunds from Shopify; see the Error Log."}

    order = (data or {}).get("order")
    if not order:
        return {"reports": [], "read": 0, "reported": 0, "duplicates": 0,
                "own_writeback": 0, "unconfirmed_writeback": 0,
                "shopify_order_id": shopify_order_id,
                "message": f"Shopify order {shopify_order_id} not found, or the token cannot "
                           "see it."}

    order_name = order.get("name") or ""
    nodes = _refund_nodes(order.get("refunds"))

    reports, duplicates, seen = [], [], set()
    for node in nodes:
        facts = facts_from_graphql(
            node,
            shopify_order_id=shopify_order_id,
            shop_domain=shop_domain,
            shopify_order_name=order_name,
        )
        key = facts.get("shopify_refund_id") or ""
        if key and key in seen:
            duplicates.append(key)
            continue
        # A node with no id at all is reported rather than deduped: two refunds
        # this app cannot name are not evidence of one refund, and reporting
        # twice is the recoverable mistake here while reporting neither is not.
        if key:
            seen.add(key)
        result = report_refund(facts)
        _surface(result, facts)
        reports.append(result)

    if duplicates:
        # No silent caps.  Named, not counted, for the reason
        # `rejection_reason` exists in refund.py: on REF-00207 the rows were
        # examined and discarded and the verdict that survived did not fit
        # them, which a bare count repeats.
        frappe.logger().info(
            "Shopify: order {order} returned {n} duplicate refund node(s) in "
            "one read ({ids}); each of those refunds was reported once.".format(
                order=order_name or shopify_order_id,
                n=len(duplicates),
                ids=", ".join(duplicates))
        )

    reported = sum(1 for r in reports if r["delivered"])
    # The deliberate skips, counted and named for the same reason the dropped
    # duplicates are.  Without this an operator read "Read 1 refund(s); 0
    # reported" and had nothing at all to explain the nought — on the EXPECTED
    # outcome for every refund this app writes back.  A count with no ids is
    # the REF-00207 failure: rows examined, discarded, and never seen again.
    own = _skipped_ids(reports, OUTCOME_OWN_WRITEBACK)
    unconfirmed = _reconcile_ids(reports)

    message = (
        "Read {read} refund(s) from Shopify order {order}; {reported} "
        "reported. Delivery is at-least-once, so that is {reported} report(s) "
        "handed over and not necessarily {reported} distinct refunds — a "
        "webhook retry or a second backfill reports the same refund again, and "
        "the observer dedupes on shopify_refund_id.".format(
            read=len(nodes), order=order_name or shopify_order_id,
            reported=reported)
    )
    if duplicates:
        message += (
            " {n} duplicate node(s) in this one read were reported once "
            "only: {ids}.".format(n=len(duplicates), ids=", ".join(duplicates))
        )
    if own:
        message += (
            " {n} refund(s) were raised here and not reported "
            "(own_writeback), because the app that would record them is the "
            "app that raised them: {ids}.".format(n=len(own),
                                                  ids="; ".join(own))
        )
    if unconfirmed:
        message += (
            " {n} of those report(s) went out while an ERPNext write-back on "
            "this order was still unconfirmed — posted, never confirmed — so "
            "whose refund it was is undecided. Each is in the Error Log with "
            "the row to resolve: {ids}.".format(n=len(unconfirmed),
                                                ids="; ".join(unconfirmed))
        )
    return {
        "shopify_order_id": shopify_order_id,
        "shopify_order_name": order_name,
        "read": len(nodes),
        "reported": reported,
        "duplicates": len(duplicates),
        # Two counts, and they mean opposite things: own_writeback was NOT
        # reported and needs nobody, while unconfirmed_writeback WAS reported
        # and needs a person to check whose refund it was.  Both are subsets of
        # different totals, so neither is "skipped" — folding them together is
        # how an operator would read a delivered report as a suppressed one.
        "own_writeback": len(own),
        "unconfirmed_writeback": len(unconfirmed),
        "reports": reports,
        "message": message,
    }


@frappe.whitelist()
def backfill_now(shopify_order_id: str, shop_domain: str = "") -> dict:
    """The HTTP door for the backfill.

    Whitelisted, unlike `report_refund` and `backfill_order_refunds`, so it can
    be driven from a form or the console.  It sends nothing to Shopify and
    cannot pay anyone — but it does hand facts to another app that may record a
    refund from them, so it is gated on Shopify Settings write: the permission
    that describes running this integration, on this app's own doctype.
    Deliberately not a role list borrowed from `payment_portals`, which would
    couple us to their configuration — the same reasoning that removed the
    permission check from `write_back_refund` in contract version 3.

    Safe to run twice, and to be expected to be: delivery is at-least-once and
    this app keeps no record of what it has already reported, so a second run
    reports the same refunds again and the observer dedupes them on
    `shopify_refund_id`.  The `reported` count it returns is reports handed
    over, not distinct refunds.

    One kind of refund comes back counted rather than reported, and it is named
    in the returned message: `own_writeback`, a refund this app raised and whose
    Shopify response it read.  A refund on an order where a write-back of ours
    was posted and never confirmed IS reported — counted separately as
    `unconfirmed_writeback` and named, because that report went out with the
    question of whose refund it was still open, and somebody may have to check.
    """
    frappe.has_permission("Shopify Settings", "write", throw=True)
    return backfill_order_refunds(shopify_order_id, shop_domain=shop_domain)
