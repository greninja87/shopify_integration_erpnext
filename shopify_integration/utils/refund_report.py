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
REPORT_CONTRACT_VERSION = 1

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
# The observer declined, or the registration is a misconfiguration.
OUTCOME_REFUSED = "refused"
# The observer raised.
OUTCOME_FAILED = "failed"

OUTCOMES = (
    OUTCOME_REPORTED,
    OUTCOME_UNACKNOWLEDGED,
    OUTCOME_NO_OBSERVER,
    OUTCOME_REFUSED,
    OUTCOME_FAILED,
)

# What this app will not conclude.  See the module docstring.
SETTLEMENT_UNDETERMINED = "undetermined"

ACK_KEY = "shopify_refund_report_accepted"
CONSUMED_KEY = "consumed"

# Facts whose silent loss changes a decision on the other side.  Every one of
# these must appear in the observer's `consumed` list for the report to count as
# delivered.
#
#   shopify_refund_id   identity; without it a second webhook is a second refund
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

    Pure: no frappe access beyond `gid`, no writes, no network.

    The one trap worth restating, because api.py already carries a comment
    about it for the log row: the payload's own ``id`` is the **refund** id.
    ``order_id`` is the order.  Conflating them files a refund against the
    wrong order.
    """
    payload = payload or {}
    txns = _successful_refund_transactions(payload.get("transactions"))
    lines = _line_items(payload.get("refund_line_items"))
    refund_id = str(payload.get("id") or "")

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
        "shopify_order_id": str(payload.get("order_id") or ""),
        "shopify_order_name": shopify_order_name or "",
        "shopify_store": shop_domain or "",

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
            unconsumed=(), recorded_as: str = "", observer: str = "") -> dict:
    """Every path returns this shape.  `report_refund` never raises."""
    return {
        "provider": PROVIDER,
        "report_contract_version": REPORT_CONTRACT_VERSION,
        "outcome": outcome,
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
    """
    facts = facts or {}
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

    return _result(
        OUTCOME_REPORTED,
        "Reported to " + observer + ".",
        facts,
        recorded_as=str(answer.get("recorded_as") or ""),
        observer=observer,
    )


# ── Frappe-bound: the webhook path ───────────────────────────────────────────

def _settings_for_store(shop_domain: str):
    """Shopify Settings for a store, or None.

    Gated on `enable_sync` only, and deliberately **not** on
    `enable_refund_writeback`.  That toggle guards the payout in `refund.py`
    and is `0` on every store, which is where it must stay; a report moves no
    money, and gating it on the payout switch would mean the one safe thing
    only ran while the dangerous thing was armed.
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
    """
    if result.get("delivered"):
        return
    if result.get("outcome") == OUTCOME_NO_OBSERVER:
        # A site without payment_portals is a normal configuration, not a fault.
        # Nothing consumes these reports there and nothing waits for one.
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
        "Not claimed    : {unclaimed}\n\n"
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


def backfill_order_refunds(shopify_order_id: str, shop_domain: str = "") -> dict:
    """Read the refunds Shopify already holds for one order, and report each.

    For the refunds that predate the webhook, and for the ones that will never
    produce a Gateway Transaction row — #6518's NEFT, every Snapmint refund —
    where Shopify is the only source that knows the refund happened at all.

    Reads Shopify and reports.  There is no mutation on this path, so it sends
    nothing to Shopify and cannot move money.
    """
    if not shopify_order_id:
        return {"reports": [], "read": 0, "reported": 0, "shopify_order_id": "",
                "message": "No Shopify order id given."}

    settings = _settings_for_store(shop_domain) if shop_domain else None
    if not settings:
        return {"reports": [], "read": 0, "reported": 0,
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
        return {"reports": [], "read": 0, "reported": 0,
                "shopify_order_id": shopify_order_id,
                "message": "Could not read refunds from Shopify; see the Error Log."}

    order = (data or {}).get("order")
    if not order:
        return {"reports": [], "read": 0, "reported": 0,
                "shopify_order_id": shopify_order_id,
                "message": f"Shopify order {shopify_order_id} not found, or the token cannot "
                           "see it."}

    order_name = order.get("name") or ""
    nodes = _refund_nodes(order.get("refunds"))

    reports = []
    for node in nodes:
        facts = facts_from_graphql(
            node,
            shopify_order_id=shopify_order_id,
            shop_domain=shop_domain,
            shopify_order_name=order_name,
        )
        result = report_refund(facts)
        _surface(result, facts)
        reports.append(result)

    reported = sum(1 for r in reports if r["delivered"])
    return {
        "shopify_order_id": shopify_order_id,
        "shopify_order_name": order_name,
        "read": len(nodes),
        "reported": reported,
        "reports": reports,
        "message": f"Read {len(nodes)} refund(s) from Shopify order {order_name or shopify_order_id}; {reported} reported.",
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
    """
    frappe.has_permission("Shopify Settings", "write", throw=True)
    return backfill_order_refunds(shopify_order_id, shop_domain=shop_domain)
