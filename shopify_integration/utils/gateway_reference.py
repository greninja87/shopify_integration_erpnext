"""
gateway_reference.py — Capture the payment gateway's transaction id onto the
Payment Entry, so gateway settlement reports can be reconciled against orders.

Why this module exists
----------------------
Shopify records the gateway's transaction id on the order's TRANSACTION record,
not on the order.  It is absent from the orders/create and orders/paid webhook
payloads we receive:

    * there is no `transactions` array on the payload
    * `reference` and `source_identifier` are null

So it has to be pulled:

    GET /admin/api/{version}/orders/{shopify_order_id}/transactions.json

Verified against a live store: Shopify's "Payment Reference" for PayU equals
PayU's own txnid exactly — order #6428 → rkdkuLhOZPiHLp9XVygf0ASij (25 chars).

That is true for PayU, whose note_attributes carry no reference.  It is NOT
true generally: a gateway wired in through a custom Shopify app is invisible to
Shopify, leaves the transaction empty or misleading, and writes its reference
into the order's note_attributes instead.

So the ORDER, not the transaction, holds the highest-ranked source, and it is
read first — a Cashfree order resolves without a transactions.json call at all.
See _REFERENCE_SOURCES for the full precedence and the evidence behind it.

What the field is for
---------------------
ONE field for every payment portal: custom_gateway_reference holds the value
that will appear as `gateway_order_ref` on the matching Gateway Transaction
(Payment Portals) row, giving a Payment Entry and a settlement line a shared
key.

That matters because most portals put the Shopify order number on their
settlement rows, so they match on it — but PayU does not.  Verified on live
data: of 681 PayU settlement rows, zero matched automatically and exactly one
was linked by hand, while Cashfree and Snapmint match on order name or platform
order id.  The reference is the only join key PayU offers.

Verified both sides for order #6428:
    Shopify transaction.authorization = rkdkuLhOZPiHLp9XVygf0ASij
    Gateway Transaction.gateway_order_ref = rkdkuLhOZPiHLp9XVygf0ASij

One caveat for whoever writes the join: gateway_order_ref is NOT unique in
Gateway Transaction — #6428 has three rows against it, two Failed and one
Success.  Any match must also require event_status = "Success", mirroring the
kind/status filter select_gateway_transaction() applies on the Shopify side.

Where the value lands
---------------------
    Payment Entry.custom_gateway_reference   the portal's order reference
    Payment Entry.custom_gateway_name        e.g. "Cards, UPI, NB by PayU India"
                                             or "CASHFREE - UPI" from the tags

`reference_no` is NOT touched.  It holds the Shopify order name (#6282) and
other code depends on that.

Writes go through frappe.db.set_value(..., update_modified=False):

    * Payment Entries are usually already submitted by the time we get here
    * it leaves `modified` alone, so no Version rows and no document churn —
      the field is filled in and nothing else about the PE changes

Failure policy
--------------
Nothing in this module is allowed to break order sync.  capture_gateway_reference
never raises: every failure path logs and returns "".  The Payment Entry and the
Sales Order stand on their own whether or not the reference could be fetched.
And a reference is never invented — if the gateway gave us nothing, the field
stays blank and the miss is logged.
"""

import json
from datetime import datetime, timezone

import frappe
from frappe.utils import cint

from shopify_integration.utils.shopify_api import (
    ShopifyAPIError,
    get_order,
    get_order_transactions,
    has_admin_api_credentials,
)

REFERENCE_FIELD = "custom_gateway_reference"
GATEWAY_FIELD   = "custom_gateway_name"

# Only a money-moving, settled transaction carries the settlement reference.
# 'authorization' is excluded: it precedes capture and its id is not what
# appears on the gateway's settlement report.
_ELIGIBLE_KINDS = ("sale", "capture")

# Where the reference lives, in priority order.  ONE table: every source lives
# here, so the precedence is readable top to bottom and adding a portal is a
# single line rather than a change to control flow.
#
# Each entry is (source_label, where, key):
#     "txn"      a field on the transaction from /transactions.json
#     "receipt"  a key inside that transaction's `receipt` blob
#     "note"     an order-level note_attributes entry, from the webhook payload
#
# Only sources verified against live settlement data belong here.  An invented
# key silently captures nothing while looking like it works.
#
# Order matters, and this is why:
#
#   pg_order_id      Cashfree, and FIRST — above authorization, which is the one
#                    place a lower rule would otherwise win wrongly.
#
#                    Partial-COD orders carry BOTH: a pg_order_id and a Shopify
#                    authorization.  Checked against the live Gateway Transaction
#                    table, those authorization tokens
#                    (rrrVTRPeGEZpqXT7sm1arpwjU, rEB8slEtDZbB24mGf2o1xDbG4 and
#                    four more) match nothing at all — neither gateway_order_ref
#                    nor gateway_payment_id.  The pg_order_ids for those same
#                    orders matched every one of the five tested, each a
#                    successful Cashfree payment.  With authorization first, 23
#                    of the 50 orders in the 29 Aug export would be given a
#                    reference that can never join, and an unjoinable reference
#                    is indistinguishable from a missing one.
#
#                    "PayU orders carry no pg_order_id" is true, but PayU is not
#                    the only gateway producing an authorization — the
#                    partial-COD checkouts produce one too, which is what made
#                    the two compete.
#
#                    Also cheap and verified: it is already in the webhook
#                    payload, so it costs no extra call, and it is char-for-char
#                    gateway_order_ref on #6485, #6488, #6489 and the #6531 test
#                    order.  Unambiguous in this data — across 337 successful
#                    Cashfree payments every order reference maps to exactly one
#                    payment.  The 20/80 splits go through partial COD, where
#                    Cashfree only ever sees the deposit and the balance is
#                    collected on delivery, so the order id still identifies one
#                    payment.
#
#                    Taken by PRESENCE, never by payment method — of the 50
#                    exported orders 33 carry it and 23 of those record as
#                    "manual", so keying off "Cashfree Payments" would catch only
#                    10 of the 33.  Putting this rule first is what lets the
#                    table stay presence-only: no gateway-name test is needed to
#                    keep authorization off a Cashfree order.
#
#   authorization    PayU's txnid.  Verified char-for-char against
#                    Gateway Transaction.gateway_order_ref on #6428.  Safe at #2
#                    because no PayU order in the export carries a pg_order_id —
#                    all 33 that do are "manual" or "Cashfree Payments" — so rule
#                    #1 never fires for PayU and this still wins for it.
#
#   cf_payment_id    Cashfree's payment id (e.g. "6350248507").  It also joins,
#                    so it is a sound fallback for an order missing
#                    pg_order_id — just not worth a receipt fetch as the primary.
#
#   txnid /          generic receipt keys other gateways use.
#   payment_id
_REFERENCE_SOURCES = (
    ("note_attributes.pg_order_id",    "note",    "pg_order_id"),
    ("transaction.authorization",      "txn",     "authorization"),
    ("receipt.cf_payment_id",          "receipt", "cf_payment_id"),
    ("receipt.txnid",                  "receipt", "txnid"),
    ("receipt.payment_id",             "receipt", "payment_id"),
)

# Kept for readability elsewhere: the note-attribute keys this app reads.
_NOTE_ATTRIBUTE_KEYS = tuple(
    key for _label, where, key in _REFERENCE_SOURCES if where == "note"
)

# Rank of the best order-level source, and a rank lookup for a source label.
# Both derive from the table, so reordering it above needs no change here.
#
# These exist for the backfill path, which starts without the order payload:
# they answer "could fetching the order still beat what I have?".  An unresolved
# source ranks worse than every real one.
_BEST_NOTE_RANK = next(
    (i for i, (_l, where, _k) in enumerate(_REFERENCE_SOURCES) if where == "note"),
    None,
)
_SOURCE_RANK = {label: i for i, (label, _w, _k) in enumerate(_REFERENCE_SOURCES)}


def _rank(source_label: str) -> int:
    """Position of a source in the table; worse than any of them when unset."""
    return _SOURCE_RANK.get(source_label, len(_REFERENCE_SOURCES))

# Strings that mean "no value" once a gateway has round-tripped its response
# through JSON.  Writing any of these would be writing a placeholder.
_NULL_TOKENS = frozenset({"", "null", "none", "nil", "n/a", "-", "undefined"})

# Payment Entry.reference_no is Data(140); keep the same ceiling.
_MAX_REFERENCE_LEN = 140


# ── Pure logic (unit-tested in tests/test_gateway_reference.py) ────────────────

def _parse_created_at(value):
    """
    Shopify `created_at` → aware datetime, or None when unparseable.

    Values arrive as ISO 8601 with an offset ("2026-08-20T14:12:03+05:30",
    sometimes "…Z").  Parsing rather than string-sorting matters: two
    transactions written in different offsets sort wrongly as strings.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Naive — assume UTC so it stays comparable with aware values.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def select_gateway_transaction(transactions, prefer_gateway: str = None):
    """
    The one transaction whose reference we want, or None.

    Eligible: kind in ("sale", "capture") AND status == "success".
    When several are eligible, the earliest by created_at wins — that is the
    original settlement, not a later top-up or re-capture.

    Transactions with a missing or unparseable created_at sort last, but stay
    eligible: a usable reference on a badly-stamped row beats no reference.

    `prefer_gateway` narrows the eligible set to transactions from one gateway
    family when any of them match.  An order can be settled by two portals — a
    part payment on one and the balance on another — and each Payment Entry
    must take the reference of the transaction that funded IT, not whichever
    came first overall.  When nothing matches, the preference is ignored rather
    than returning None: an unmatched preference must not lose a reference that
    the unfiltered rule would have found.
    """
    candidates = []
    for txn in (transactions or []):
        if not isinstance(txn, dict):
            continue
        if (txn.get("kind") or "").strip().lower() not in _ELIGIBLE_KINDS:
            continue
        if (txn.get("status") or "").strip().lower() != "success":
            continue
        candidates.append(txn)

    if not candidates:
        return None

    wanted = _gateway_family(prefer_gateway)
    if wanted:
        matching = [t for t in candidates
                    if _gateway_family(t.get("gateway")) == wanted]
        if matching:
            candidates = matching

    def sort_key(txn):
        parsed = _parse_created_at(txn.get("created_at"))
        # (has_no_timestamp, timestamp) — None sorts last without comparing None.
        return (parsed is None, parsed or datetime.min.replace(tzinfo=timezone.utc))

    return sorted(candidates, key=sort_key)[0]


def _clean(value) -> str:
    """Normalise a candidate reference; "" when it is not a real value."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if cleaned.lower() in _NULL_TOKENS:
        return ""
    return cleaned


def _receipt(txn) -> dict:
    """
    The transaction's receipt as a dict.

    Most gateways give an object; some serialise it as a JSON string.  Anything
    else (list, number, unparseable string) yields {}.
    """
    receipt = txn.get("receipt")
    if isinstance(receipt, dict):
        return receipt
    if isinstance(receipt, str) and receipt.strip():
        try:
            parsed = json.loads(receipt)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _note_attribute(order, key: str) -> str:
    """One order-level note_attributes value by name, or ""."""
    if not isinstance(order, dict):
        return ""
    for attr in (order.get("note_attributes") or []):
        if isinstance(attr, dict) and (attr.get("name") or "").strip() == key:
            return _clean(attr.get("value"))
    return ""


def extract_gateway_reference(txn, order=None) -> str:
    """
    The gateway reference for one payment, or "".

    ONE value for every portal: whatever will appear as `gateway_order_ref` on
    the matching Gateway Transaction row, so a Payment Entry and a settlement
    line share a key.  Where that value lives on the Shopify side differs by
    portal, so the sources are tried in the order declared by
    _REFERENCE_SOURCES — see the reasoning recorded there.

    The value is written through VERBATIM.  Cashfree's pg_order_id carries the
    shop domain ("notdrones.myshopify.com_flytsieopj") and the settlement's
    gateway_order_ref carries it too, so stripping the prefix would break the
    join it exists for.

    Snapmint is deliberately unhandled: its gateway_order_ref is a 10-digit id
    with no counterpart in the Shopify payload, and its settlement rows already
    carry platform_order_name, so they match on order name without help.

    Returns "" rather than a placeholder when every source is empty.

    :param txn:   one transaction from GET /orders/{id}/transactions.json
    :param order: the order payload, carrying the note_attributes sources.
                  Omit it and only the transaction-level sources are tried.
    """
    return _resolve_reference(txn, order)[0]


def _resolve_reference(txn, order, skip_order_sources: bool = False) -> tuple:
    """
    (reference, source_label) — "" and "" when no source yields a value.

    Separate from extract_gateway_reference so the diagnostic log can say WHICH
    rule produced a value, without the caller having to re-derive it.

    `skip_order_sources` drops the order-level sources, leaving only the ones
    read off the transaction.  Set when the payment came through a different
    gateway than the order's own: note_attributes.pg_order_id is written by one
    portal's app and describes one portal's payment, so on a split-gateway order
    it is simply not this payment's reference — outranking the transaction with
    it produced a key that joins to the wrong settlement row.
    """
    for label, where, key in _REFERENCE_SOURCES:
        if where == "note" and skip_order_sources:
            continue
        if where == "txn":
            candidate = _clean(txn.get(key)) if isinstance(txn, dict) else ""
        elif where == "receipt":
            candidate = _clean(_receipt(txn).get(key)) if isinstance(txn, dict) else ""
        else:
            candidate = _note_attribute(order, key)

        if candidate:
            return candidate[:_MAX_REFERENCE_LEN], label

    return "", ""


def extract_gateway_name(txn, order=None) -> str:
    """
    Which portal took the payment, e.g. 'Cards, UPI, NB by PayU India'.

    `transaction.gateway` reads "manual" for a gateway integrated through a
    custom app, which is worse than useless on a reconciliation field — it hides
    that the payment was Cashfree.  So "manual" is treated as absent and the
    order's tags are consulted instead, which is where those integrations put
    the real gateway (e.g. "CASHFREE - UPI").  Same reasoning as the tag-first
    matching in payment_entry._resolve_gateway_mapping().

    Returns "" when the only thing on offer IS "manual" — with no order to read
    tags from, there is nothing to say.  Writing "manual" onto a field described
    as "identifies which settlement portal the reference belongs to" would be a
    placeholder: it names no portal while looking like captured data.
    """
    gateway = _clean(txn.get("gateway")) if isinstance(txn, dict) else ""
    if gateway and gateway.lower() != "manual":
        return gateway[:_MAX_REFERENCE_LEN]

    if isinstance(order, dict):
        tags = _clean(order.get("tags"))
        if tags:
            return tags[:_MAX_REFERENCE_LEN]
        names = order.get("payment_gateway_names") or []
        if names:
            first = _clean(names[0])
            if first:
                return first[:_MAX_REFERENCE_LEN]

    if gateway.lower() == "manual":
        return ""
    return gateway[:_MAX_REFERENCE_LEN]


# ── Field availability ────────────────────────────────────────────────────────

def _pe_has_field(fieldname: str) -> bool:
    """
    Whether Payment Entry actually carries a field.

    after_install / the add_payment_entry_gateway_fields patch create both, but
    an install that has not migrated yet must degrade quietly rather than crash
    every order.
    """
    try:
        return bool(frappe.get_meta("Payment Entry").has_field(fieldname))
    except Exception:
        return False


# ── Capture ───────────────────────────────────────────────────────────────────

def capture_gateway_reference(
    pe_name: str,
    shopify_order_id,
    settings=None,
    transactions=None,
    order=None,
) -> str:
    """
    Fetch and store the gateway reference for one Payment Entry.

    Idempotent: returns immediately when custom_gateway_reference is already
    set, so re-running order sync or the backfill never re-fetches or overwrites.

    Never raises — every failure is logged and "" returned, so the caller's
    Payment Entry and Sales Order are unaffected.

    :param pe_name:          Payment Entry name
    :param shopify_order_id: numeric Shopify order id
    :param settings:         Shopify Settings doc; resolved from the linked
                             Sales Order when omitted
    :param transactions:     pre-fetched transactions list (used by the
                             backfill and by tests) to skip the HTTP call
    :param order:            the order payload, carrying the highest-ranked
                             source in _REFERENCE_SOURCES.  Supplied free by the
                             order-sync path; fetched first when absent, which
                             is why an order whose note_attributes resolve never
                             costs a /transactions.json call at all.
    :return: the reference written, or "" when nothing was written
    """
    try:
        if not pe_name:
            return ""

        order_id = str(shopify_order_id or "").strip()
        if not order_id:
            frappe.log_error(
                f"Gateway reference skipped for Payment Entry {pe_name}: "
                f"no Shopify order id available.",
                "Shopify: Gateway Reference Skipped (No Order ID)",
            )
            return ""

        if not _pe_has_field(REFERENCE_FIELD):
            frappe.log_error(
                f"Payment Entry has no '{REFERENCE_FIELD}' field — gateway reference "
                f"not captured for {pe_name}. Run `bench --site <site> migrate` "
                f"(or reinstall the app) to create the Shopify custom fields.",
                "Shopify: Gateway Reference Field Missing",
            )
            return ""

        # ── Idempotency guard ────────────────────────────────────────────────
        existing = (frappe.db.get_value("Payment Entry", pe_name, REFERENCE_FIELD) or "").strip()
        if existing:
            return existing

        # ── Submitted only ───────────────────────────────────────────────────
        # Enforced here as well as in the backfill query, so a direct call
        # cannot bypass it.  Silent rather than logged: a draft Payment Entry is
        # a normal intermediate state, not a fault, and the order-sync path
        # reaches this line for every store that leaves auto-submit off.  The
        # entry is picked up by the backfill once it is submitted.
        if cint(frappe.db.get_value("Payment Entry", pe_name, "docstatus")) != 1:
            return ""

        # ── Resolve store settings ───────────────────────────────────────────
        if settings is None:
            settings = _settings_for_payment_entry(pe_name)
        if not settings:
            frappe.log_error(
                f"Gateway reference skipped for Payment Entry {pe_name}: "
                f"could not resolve the Shopify store for order {order_id}.",
                "Shopify: Gateway Reference Skipped (No Store)",
            )
            return ""

        # No token configured → the feature is simply off for this store.
        # Silent by design: logging every order would be noise, not signal.
        #
        # The test covers BOTH calls.  The order is fetched first now, so gating
        # on `transactions is None` alone would let a caller that supplies
        # transactions still reach Shopify for the order on a store that has no
        # credentials to reach it with.
        needs_shopify = transactions is None or order is None
        if needs_shopify and not has_admin_api_credentials(settings):
            return ""

        # ── Fetch, best source first ─────────────────────────────────────────
        # The order carries the top-ranked source (pg_order_id), so it is
        # consulted BEFORE /transactions.json rather than after.  Two things
        # follow, and both were bugs when the order came last:
        #
        #   * an order with no successful sale/capture transaction still yields
        #     its reference — the old code hit the "no usable transaction"
        #     return below and gave up with the value sitting in the payload
        #   * when the order resolves, the transactions call is never made, so
        #     a Cashfree entry now costs ONE request instead of two against the
        #     2 req/sec budget
        order_fetch_failed = False
        if order is None and _BEST_NOTE_RANK == 0:
            order, order_fetch_failed = _fetch_order(settings, order_id, pe_name)

        # ── Which gateway funded THIS payment? ───────────────────────────────
        # The account the money landed in, mapped back through the Gateway
        # Mapping.  When it disagrees with the gateway Shopify recorded on the
        # order, the order was settled by two portals and the order-level
        # sources describe the other one — so they are skipped for this payment
        # and the matching transaction is used instead.
        #
        # Live case: order #6138 was part-paid by Cashfree (1,999.80) and the
        # balance by PayU (7,999.20).  Both Payment Entries took Cashfree's
        # pg_order_id, so the PayU payment carried a key that joined to
        # Cashfree's settlement row.
        pe_account = frappe.db.get_value("Payment Entry", pe_name, "paid_to") or ""
        pe_gateway = _gateway_for_account(pe_account)
        order_gateway = _gateway_family(extract_gateway_name(None, order))
        cross_gateway = bool(pe_gateway and order_gateway and pe_gateway != order_gateway)

        reference, source = _resolve_reference(
            None, order, skip_order_sources=cross_gateway
        )

        # Rank 0 is the best the table can do, so anything worse means the
        # transaction is still worth asking for.  A cross-gateway payment always
        # asks, because only the transaction can identify its own settlement.
        txn = None
        if cross_gateway or _rank(source) > 0:
            if transactions is None:
                transactions = get_order_transactions(settings, order_id)
            txn = select_gateway_transaction(transactions, prefer_gateway=pe_gateway)
            reference, source = _resolve_reference(
                txn, order, skip_order_sources=cross_gateway
            )

        # No usable transaction, and the order did not supply one either.  A
        # missing transaction is not itself fatal — that is why the order is
        # consulted first — so reaching here means both sources are genuinely
        # empty, or the order could not be read.
        if not txn and not reference:
            frappe.log_error(
                f"No successful sale/capture transaction on Shopify order {order_id} "
                f"(Payment Entry {pe_name}), and no gateway reference from the order "
                f"either ({'could not be fetched' if order_fetch_failed else 'no note_attributes match'}). "
                f"{len(transactions or [])} transaction(s) returned. "
                f"Gateway reference left blank.",
                "Shopify: Gateway Reference Not Found",
            )
            return ""

        # The order still has not been consulted and it holds a source that
        # outranks what we have.  Unreachable while the table puts an
        # order-level source at rank 0 (the block above already fetched it), and
        # kept so that reordering the table cannot silently skip the order.
        if (
            order is None
            and not order_fetch_failed
            and _BEST_NOTE_RANK is not None
            and _BEST_NOTE_RANK < _rank(source)
        ):
            order, order_fetch_failed = _fetch_order(settings, order_id, pe_name)
            fetched, fetched_source = _resolve_reference(
                txn, order, skip_order_sources=cross_gateway
            )
            if fetched:
                reference, source = fetched, fetched_source

        # A source that outranks what we hold could not be checked.  Do NOT
        # settle for the lower-ranked value: this write is effectively permanent
        # — the idempotency guard returns early on a filled field and the
        # backfill query only selects blank ones — so a transient 500 would bake
        # in, say, a partial-COD authorization that joins to nothing, and no
        # re-run would ever correct it.  Blank is retryable; wrong is not.
        if (
            order_fetch_failed
            and _BEST_NOTE_RANK is not None
            and _BEST_NOTE_RANK < _rank(source)
        ):
            return ""

        if not reference:
            frappe.log_error(
                "\n".join([
                    f"Shopify order {order_id} (Payment Entry {pe_name}): transaction "
                    f"{(txn or {}).get('id')} carries no gateway reference, and none "
                    f"in the order payload either.",
                    "",
                    f"gateway            : {(txn or {}).get('gateway') or '(blank)'}",
                    f"authorization      : {(txn or {}).get('authorization') or '(blank)'}",
                    f"receipt keys       : {sorted(_receipt(txn or {}).keys()) or '(none)'}",
                    f"order tags         : {(order or {}).get('tags') or '(blank)'}",
                    f"note_attributes    : "
                    f"{sorted((a or {}).get('name') for a in ((order or {}).get('note_attributes') or []) if isinstance(a, dict)) or '(none)'}",
                    f"sources tried      : {[label for label, _w, _k in _REFERENCE_SOURCES]}",
                    "",
                    f"{REFERENCE_FIELD} left blank — no placeholder written.",
                ]),
                "Shopify: Gateway Reference Empty",
            )
            return ""

        # ── Write ────────────────────────────────────────────────────────────
        values = {REFERENCE_FIELD: reference}

        # On a cross-gateway payment the order's tags name the OTHER portal, so
        # the transaction is the only honest source for this payment's name.
        gateway_name = extract_gateway_name(txn, None if cross_gateway else order)
        if not gateway_name and cross_gateway:
            gateway_name = pe_account
        if gateway_name and _pe_has_field(GATEWAY_FIELD):
            values[GATEWAY_FIELD] = gateway_name

        # update_modified=False: fills the field without bumping `modified` or
        # creating a Version row, on submitted documents included.
        frappe.db.set_value("Payment Entry", pe_name, values, update_modified=False)

        # Commit the reference on its own.  In the order-sync flow the Sales
        # Order and Payment Entry are already committed by the time we get here,
        # but Sales Invoice creation runs afterwards — a rollback there must not
        # take this write with it.
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; reference must survive later rollbacks

        frappe.logger().info(
            f"Shopify: gateway reference {reference} captured on Payment Entry "
            f"{pe_name} (order {order_id}) from {source or 'unknown source'}."
        )
        return reference

    except ShopifyAPIError as exc:
        frappe.log_error(
            f"Gateway reference fetch failed for Payment Entry {pe_name} "
            f"(Shopify order {shopify_order_id}): {exc}\n\n"
            f"The Payment Entry itself is unaffected. Re-run the backfill "
            f"(shopify_integration.utils.gateway_reference.backfill_gateway_references) "
            f"once the cause is fixed.",
            "Shopify: Gateway Reference API Error",
        )
        return ""
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Gateway Reference Failed — {pe_name}",
        )
        return ""


def _fetch_order(settings, order_id: str, pe_name: str) -> tuple:
    """
    (order, failed) — the Shopify order, or (None, True) when it could not be
    fetched.

    The flag matters more than the None: it tells the caller that a source it
    has not seen might outrank the one it holds, which is the difference between
    "this order genuinely has no reference" and "we could not look".
    """
    try:
        return get_order(settings, order_id), False
    except ShopifyAPIError as exc:
        frappe.log_error(
            f"Could not fetch Shopify order {order_id} to look for a gateway "
            f"reference in note_attributes (Payment Entry {pe_name}): {exc}",
            "Shopify: Gateway Reference Order Fetch Failed",
        )
        return None, True


def _settings_for_payment_entry(pe_name: str):
    """
    Shopify Settings for the store that produced a Payment Entry.

    Payment Entry carries no Shopify store field, so the store is resolved from
    the Sales Order, by either of two routes:

      1. the PE's Sales Order reference row, and
      2. PE.reference_no, which holds the Shopify order name (#6119) and matches
         Sales Order.po_no.

    Route 2 exists because ERPNext's advance allocation re-points a PE's
    reference from the Sales Order to the Sales Invoice when that invoice is
    submitted.  After that the PE has no Sales Order reference row at all, and
    route 1 alone finds nothing — measured on live data, 8,587 Payment Entries
    reference an invoice against 455 that still reference an order.
    """
    row = frappe.db.sql(
        """
        SELECT so.shopify_store
        FROM `tabPayment Entry Reference` per
        JOIN `tabSales Order` so ON so.name = per.reference_name
        WHERE per.parent = %(pe)s
          AND per.reference_doctype = 'Sales Order'
          AND IFNULL(so.shopify_store, '') != ''

        UNION

        SELECT so.shopify_store
        FROM `tabPayment Entry` pe
        JOIN `tabSales Order` so
              ON so.po_no = pe.reference_no
             AND so.docstatus != 2
        WHERE pe.name = %(pe)s
          AND IFNULL(pe.reference_no, '') != ''
          AND IFNULL(so.shopify_store, '') != ''

        UNION

        SELECT so.shopify_store
        FROM `tabPayment Entry Reference` per
        JOIN `tabSales Invoice` si
              ON si.name = per.reference_name
        JOIN `tabSales Order` so
              ON so.po_no = si.po_no
             AND so.docstatus != 2
        WHERE per.parent = %(pe)s
          AND per.reference_doctype = 'Sales Invoice'
          AND IFNULL(si.po_no, '') != ''
          AND IFNULL(so.shopify_store, '') != ''

        LIMIT 1
        """,
        {"pe": pe_name},
        as_dict=True,
    )
    if not row:
        return None

    store_name = frappe.db.get_value(
        "Shopify Settings", {"shop_domain": row[0]["shopify_store"]}, "name"
    )
    return frappe.get_doc("Shopify Settings", store_name) if store_name else None


# ── Order-sync entry point ────────────────────────────────────────────────────

def capture_for_order(pe_name: str, order: dict, settings) -> str:
    """
    Called right after the order sync creates or updates a Payment Entry.

    Thin wrapper over capture_gateway_reference that pulls the Shopify order id
    out of the webhook payload.  Same guarantee: never raises.
    """
    if not pe_name:
        return ""
    return capture_gateway_reference(
        pe_name,
        (order or {}).get("id"),
        settings=settings,
        order=order,
    )


# ── Backfill ──────────────────────────────────────────────────────────────────

def _gateway_family(value: str) -> str:
    """Coarse gateway identity for comparison, or "" when unrecognised.

    Deliberately coarse.  The same portal appears under many spellings across
    the three places it is named -- "Cashfree Payments" in
    payment_gateway_names, "CASHFREE - UPI" in tags, "Cashfree" as the
    Gateway Transaction provider, "CashFree A/C - NDIPL" as the account -- and
    an exact match on any pair of those would fail.  Only the portal matters
    here, never the instrument.
    """
    text = (value or "").lower()
    for family in ("cashfree", "snapmint", "payu", "razorpay", "paytm", "phonepe"):
        if family in text:
            return family
    return ""


def _gateway_for_account(account: str) -> str:
    """The gateway family configured against a bank account, or "".

    Read from the Gateway Mapping rows rather than the account name, so a
    renamed account keeps working; falls back to the account name because that
    is how these accounts are conventionally named ("PayU Payments Private
    Limited - NDIPL") and it costs nothing to try.
    """
    if not account:
        return ""

    rows = frappe.db.sql(
        """
        SELECT shopify_gateway, tag_contains
        FROM `tabShopify Payment Gateway Mapping`
        WHERE bank_account = %(account)s
        """,
        {"account": account},
        as_dict=True,
    )
    for row in rows:
        family = (_gateway_family(row.get("shopify_gateway"))
                  or _gateway_family(row.get("tag_contains")))
        if family:
            return family

    return _gateway_family(account)


def _gateway_bank_accounts() -> list:
    """
    Accounts that Shopify Settings treats as payment-gateway destinations: every
    bank_account on a Gateway Mapping row, plus each store's default.

    Read from configuration rather than hardcoded, so adding a gateway in
    Settings is enough and no account name is baked into the app.

    Used to restrict the Sales-Invoice resolution route.  That route matches on
    Sales Invoice.po_no, which is broad enough to catch any manual payment
    allocated to a Shopify order's invoice -- including bank transfers and COD
    remittances that have no gateway reference at all.  Confining it to money
    that actually landed in a gateway account keeps those out.
    """
    accounts = set()
    for row in frappe.db.sql(
        """
        SELECT DISTINCT bank_account
        FROM `tabShopify Payment Gateway Mapping`
        WHERE IFNULL(bank_account, '') != ''
        """,
        as_dict=True,
    ):
        accounts.add(row["bank_account"])
    for row in frappe.db.sql(
        """
        SELECT DISTINCT default_bank_account
        FROM `tabShopify Settings`
        WHERE IFNULL(default_bank_account, '') != ''
        """,
        as_dict=True,
    ):
        accounts.add(row["default_bank_account"])
    return sorted(accounts)


def _pending_payment_entries(store: str = None, limit: int = 200, from_date: str = None) -> list:
    """
    Shopify-created Payment Entries with no gateway reference yet, oldest first.

    Payment Entry has no Shopify field of its own, so "Shopify-created" is
    established by reaching a Sales Order that carries a shopify_order_id.  Two
    routes are needed, UNIONed:

      1. the PE's Sales Order reference row, and
      2. PE.reference_no, which create_payment_entry_from_shopify() sets to the
         Shopify order name (#6119), matched against Sales Order.po_no.

      3. the PE's Sales Invoice reference -> Sales Invoice.po_no -> Sales Order,
         restricted to Payment Entries paid into a configured gateway account
         (see _gateway_bank_accounts).

    Route 2 is not a nicety.  When a Sales Invoice is submitted with
    allocate_advances_automatically, ERPNext moves the PE's reference off the
    Sales Order and onto the invoice, after which route 1 cannot see the PE at
    all.  Measured on live data: 8,587 Payment Entries reference an invoice
    against 455 that still reference an order, so route 1 alone misses the
    overwhelming majority of well-invoiced orders.

    po_no is a safe key: across 1,594 Shopify Sales Orders there are 1,590
    distinct values, no value is shared between the two stores, and the three
    reused values are amend chains where every copy but one is cancelled --
    which `so.docstatus != 2` removes.

    Route 3 exists for gateway payments entered by hand.  Those carry a typed
    note in reference_no ("CF", "cashfree", "snapmint") rather than the order
    name, and reference only the invoice, so routes 1 and 2 both miss them --
    measured on live data, 44 such payments in August alone, every one of them
    on a Shopify order whose po_no is an internal number (EB3436) rather than a
    "#" name.  It is deliberately the narrowest route: without the gateway-account
    restriction it would also claim bank transfers and COD remittances that
    happen to be allocated to a Shopify order's invoice.

    Only SUBMITTED Payment Entries (docstatus 1) are selected -- drafts and
    cancelled entries are both skipped.  A draft is not yet a payment: it can
    still be edited, re-pointed at a different order, or abandoned, so keying it
    to a settlement row would leave a reference behind that outlives the
    document it described.  Live case: a draft COD remittance sat allocated to
    a Shopify order and re-acquired that order's Cashfree reference on every
    run, having been cleared by hand each time.  It becomes eligible the moment
    it is submitted.

    order_count is returned so the caller can skip a PE that resolves to more
    than one Shopify order rather than stamping one order's reference onto a
    payment covering several.

    :param from_date: only Payment Entries posted on/after this date (YYYY-MM-DD)
    """
    conditions = ""
    params = {"limit": int(limit)}
    if store:
        conditions += " AND src.shopify_store = %(store)s"
        params["store"] = store
    if from_date:
        conditions += " AND pe.posting_date >= %(from_date)s"
        params["from_date"] = from_date

    # Route 3 is only built when gateway accounts are configured.  An empty
    # allowlist must match nothing, never everything.
    gw_accounts = _gateway_bank_accounts()
    invoice_route = ""
    if gw_accounts:
        params["gw_accounts"] = gw_accounts
        invoice_route = f"""
            UNION

            SELECT pe3.name AS pe_name, so.shopify_order_id, so.shopify_store
            FROM `tabPayment Entry` pe3
            JOIN `tabPayment Entry Reference` per3
                  ON per3.parent = pe3.name
                 AND per3.reference_doctype = 'Sales Invoice'
            JOIN `tabSales Invoice` si
                  ON si.name = per3.reference_name
            JOIN `tabSales Order` so
                  ON so.po_no = si.po_no
                 AND so.docstatus != 2
            WHERE pe3.docstatus = 1
              AND IFNULL(pe3.{REFERENCE_FIELD}, '') = ''
              AND pe3.paid_to IN %(gw_accounts)s
              AND IFNULL(si.po_no, '') != ''
              AND IFNULL(so.shopify_order_id, '') != ''
        """

    return frappe.db.sql(
        f"""
        SELECT
            pe.name                               AS pe_name,
            MIN(src.shopify_order_id)             AS shopify_order_id,
            MIN(src.shopify_store)                AS shopify_store,
            COUNT(DISTINCT src.shopify_order_id)  AS order_count
        FROM `tabPayment Entry` pe
        JOIN (
            SELECT per.parent AS pe_name, so.shopify_order_id, so.shopify_store
            FROM `tabPayment Entry Reference` per
            JOIN `tabSales Order` so
                  ON so.name = per.reference_name
            WHERE per.reference_doctype = 'Sales Order'
              AND IFNULL(so.shopify_order_id, '') != ''

            UNION

            SELECT pe2.name AS pe_name, so.shopify_order_id, so.shopify_store
            FROM `tabPayment Entry` pe2
            JOIN `tabSales Order` so
                  ON so.po_no = pe2.reference_no
                 AND so.docstatus != 2
            WHERE pe2.docstatus = 1
              AND IFNULL(pe2.reference_no, '') != ''
              AND IFNULL(pe2.{REFERENCE_FIELD}, '') = ''
              AND IFNULL(so.shopify_order_id, '') != ''
            {invoice_route}
        ) src ON src.pe_name = pe.name
        WHERE pe.docstatus = 1
          AND IFNULL(pe.{REFERENCE_FIELD}, '') = ''
          {conditions}
        GROUP BY pe.name, pe.creation
        ORDER BY pe.creation ASC
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )


@frappe.whitelist()
def backfill_gateway_references(
    store: str = None, limit: int = 200, dry_run: int = 0, from_date: str = None
) -> dict:
    """
    Populate custom_gateway_reference on existing Shopify Payment Entries,
    oldest first.

    Runs independently of order sync and is safe to re-run: entries that
    already have a reference are excluded by the query, and
    capture_gateway_reference re-checks before writing.  Nothing other than the
    two gateway fields is written, and `modified` is left untouched.

    Rate limiting (2 req/sec + 429 retry) is enforced inside shopify_api, so a
    long run paces itself.

    From the CLI:

        bench --site <site> execute \\
          shopify_integration.utils.gateway_reference.backfill_gateway_references \\
          --kwargs "{'limit': 500}"

    Dry run first to see the scope without any writes:

        --kwargs "{'limit': 500, 'dry_run': 1}"

    :param store:     shop_domain to restrict to; all stores when omitted
    :param limit:     maximum Payment Entries to process this run
    :param dry_run:   when truthy, report what would be processed and write nothing
    :param from_date: only Payment Entries posted on/after this date (YYYY-MM-DD).
                      Without it the run walks the whole history oldest-first and
                      a small `limit` never reaches recent orders.
    :return: {"scanned", "updated", "no_reference", "ambiguous", "failed",
              "dry_run", "entries"}
    """
    limit   = cint(limit) or 200
    dry_run = bool(cint(dry_run))

    result = {
        "scanned": 0, "updated": 0, "no_reference": 0, "ambiguous": 0,
        "failed": 0, "dry_run": dry_run, "entries": [],
    }

    if not _pe_has_field(REFERENCE_FIELD):
        frappe.log_error(
            f"Backfill aborted: Payment Entry has no '{REFERENCE_FIELD}' field. "
            f"Run `bench --site <site> migrate` first.",
            "Shopify: Gateway Reference Backfill Aborted",
        )
        result["aborted"] = f"Payment Entry has no '{REFERENCE_FIELD}' field."
        return result

    rows = _pending_payment_entries(store=store, limit=limit, from_date=from_date)
    result["scanned"] = len(rows)

    # One Settings doc per store, not per Payment Entry.
    settings_cache = {}

    for row in rows:
        pe_name  = row["pe_name"]
        order_id = row["shopify_order_id"]
        domain   = row.get("shopify_store") or ""

        # A Payment Entry covering several Shopify orders has several gateway
        # references and one field to hold them.  Picking one would stamp a
        # settlement key that is wrong for every other order on the payment, so
        # it is skipped and reported instead.  Not silent: a wrong reference on
        # a reconciliation join is worse than a blank one.
        if cint(row.get("order_count") or 1) > 1:
            result["ambiguous"] += 1
            frappe.log_error(
                f"Payment Entry {pe_name} resolves to {row.get('order_count')} "
                f"different Shopify orders — gateway reference not set. A "
                f"consolidated payment needs the reference chosen by hand.",
                "Shopify: Gateway Reference Ambiguous",
            )
            continue

        if dry_run:
            result["entries"].append({"payment_entry": pe_name, "shopify_order_id": order_id})
            continue

        if domain not in settings_cache:
            settings_cache[domain] = _settings_for_domain(domain)
        settings = settings_cache[domain]

        if not settings:
            result["failed"] += 1
            continue

        if not has_admin_api_credentials(settings):
            frappe.log_error(
                f"Backfill skipped store '{domain}': no Admin API access token "
                f"configured (Shopify Settings → Connection → Shopify Admin API).",
                "Shopify: Gateway Reference Backfill Skipped (No Token)",
            )
            result["failed"] += 1
            continue

        reference = capture_gateway_reference(pe_name, order_id, settings=settings)
        if reference:
            result["updated"] += 1
            result["entries"].append({
                "payment_entry": pe_name,
                "shopify_order_id": order_id,
                "reference": reference,
            })
        else:
            result["no_reference"] += 1

        # Commit periodically so a long run's progress survives an interruption
        # and is not all rolled back.
        if result["updated"] and result["updated"] % 20 == 0:
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — long-running backfill; progress must persist

    if not dry_run:
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; writes must persist
        frappe.logger().info(
            f"Shopify gateway reference backfill: scanned {result['scanned']}, "
            f"updated {result['updated']}, no reference {result['no_reference']}, "
            f"failed {result['failed']}."
        )

    return result


def _settings_for_domain(shop_domain: str):
    """Shopify Settings doc for a shop domain, or None."""
    if not shop_domain:
        return None
    name = frappe.db.get_value("Shopify Settings", {"shop_domain": shop_domain}, "name")
    return frappe.get_doc("Shopify Settings", name) if name else None


@frappe.whitelist()
def enqueue_backfill(store: str = None, limit: int = 200, from_date: str = None) -> str:
    """
    Run the backfill in a background job.

    Preferred over calling backfill_gateway_references() from the UI: pacing at
    2 req/sec means a few hundred entries take minutes, which would time out a
    web request.
    """
    limit = cint(limit) or 200
    frappe.enqueue(
        "shopify_integration.utils.gateway_reference.backfill_gateway_references",
        queue="long",
        timeout=max(600, limit * 10),   # ~0.5 s/request plus headroom for retries
        store=store,
        limit=limit,
        from_date=from_date,
        job_name=f"shopify_gateway_reference_backfill_{store or 'all'}",
    )
    return f"Gateway reference backfill queued for up to {limit} Payment Entries."
