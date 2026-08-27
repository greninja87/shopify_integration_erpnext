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
Shopify, leaves the transaction empty, and writes its reference into the order's
note_attributes instead — so the order payload is a second source, not a dead
end.  See _NOTE_ATTRIBUTE_KEYS.

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

# Where the reference lives, in priority order.  `authorization` is Shopify's
# own normalised home for the gateway reference; the receipt blob is the
# gateway's raw response and differs per provider.
_REFERENCE_PATHS = (
    ("authorization",),
    ("receipt", "txnid"),
    ("receipt", "payment_id"),
)

# Order-level note_attributes keys carrying a gateway reference.
#
# Needed because a gateway integrated through a custom Shopify app is invisible
# to Shopify: payment_gateway_names reads ["manual"], the transaction has no
# `authorization` and an empty `receipt`, and the real reference is left in the
# order's note_attributes instead.
#
# Only keys verified against live settlement data belong here — an invented key
# would silently capture nothing.  Verified: Cashfree writes `pg_order_id`
# (e.g. "notdrones.myshopify.com_lgbqpnzdkq"), which is byte-for-byte the
# `gateway_order_ref` on the matching Gateway Transaction row.
_NOTE_ATTRIBUTE_KEYS = ("pg_order_id",)

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


def select_gateway_transaction(transactions):
    """
    The one transaction whose reference we want, or None.

    Eligible: kind in ("sale", "capture") AND status == "success".
    When several are eligible, the earliest by created_at wins — that is the
    original settlement, not a later top-up or re-capture.

    Transactions with a missing or unparseable created_at sort last, but stay
    eligible: a usable reference on a badly-stamped row beats no reference.
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
    portal, which is why this is a resolution chain rather than a single field:

        transaction.authorization      PayU — verified: the 25-char txnid, and
                                       the same string as gateway_order_ref
        receipt.txnid / .payment_id    gateways that return a receipt blob
        note_attributes.pg_order_id    Cashfree via a custom app, which reports
                                       payment_gateway_names ["manual"] and
                                       leaves the transaction empty

    Snapmint is deliberately unhandled: its gateway_order_ref is a 10-digit id
    with no counterpart in the Shopify payload, and its settlement rows already
    carry platform_order_name so they match on order name without help.

    Returns "" rather than a placeholder when every source is empty.

    :param txn:   one transaction from GET /orders/{id}/transactions.json
    :param order: the order payload, for the note_attributes fallback.  Omit it
                  and only the transaction-level sources are tried.
    """
    if isinstance(txn, dict):
        for path in _REFERENCE_PATHS:
            if len(path) == 1:
                candidate = _clean(txn.get(path[0]))
            else:
                candidate = _clean(_receipt(txn).get(path[1]))
            if candidate:
                return candidate[:_MAX_REFERENCE_LEN]

    for key in _NOTE_ATTRIBUTE_KEYS:
        candidate = _note_attribute(order, key)
        if candidate:
            return candidate[:_MAX_REFERENCE_LEN]

    return ""


def extract_gateway_name(txn, order=None) -> str:
    """
    Which portal took the payment, e.g. 'Cards, UPI, NB by PayU India'.

    `transaction.gateway` reads "manual" for a gateway integrated through a
    custom app, which is worse than useless on a reconciliation field — it hides
    that the payment was Cashfree.  So "manual" is treated as absent and the
    order's tags are consulted instead, which is where those integrations put
    the real gateway (e.g. "CASHFREE - UPI").  Same reasoning as the tag-first
    matching in payment_entry._resolve_gateway_mapping().
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
    :param order:            the order payload, for the note_attributes
                             fallback.  Supplied free by the order-sync path;
                             the backfill fetches it only when the transaction
                             yields nothing.
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
        if transactions is None and not has_admin_api_credentials(settings):
            return ""

        # ── Fetch ────────────────────────────────────────────────────────────
        if transactions is None:
            transactions = get_order_transactions(settings, order_id)

        txn = select_gateway_transaction(transactions)

        reference = extract_gateway_reference(txn, order)

        # No usable transaction AND nothing in the order: genuinely nothing to
        # capture.  A missing transaction is not itself fatal — a Cashfree order
        # reports payment_gateway_names ["manual"] and its reference lives in the
        # order's note_attributes, so the order alone can still supply it.
        if not txn and not reference:
            frappe.log_error(
                f"No successful sale/capture transaction on Shopify order {order_id} "
                f"(Payment Entry {pe_name}), and no gateway reference in the order "
                f"payload. {len(transactions or [])} transaction(s) returned. "
                f"Gateway reference left blank.",
                "Shopify: Gateway Reference Not Found",
            )
            return ""

        # Transaction empty, order not yet consulted — fetch it before giving up.
        # Only reached on the backfill path; the order-sync path passes `order` in.
        if not reference and order is None and _NOTE_ATTRIBUTE_KEYS:
            try:
                order = get_order(settings, order_id)
                reference = extract_gateway_reference(txn, order)
            except ShopifyAPIError as exc:
                frappe.log_error(
                    f"Could not fetch Shopify order {order_id} to look for a "
                    f"gateway reference in note_attributes "
                    f"(Payment Entry {pe_name}): {exc}",
                    "Shopify: Gateway Reference Order Fetch Failed",
                )

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
                    f"looked for keys    : {list(_NOTE_ATTRIBUTE_KEYS)}",
                    "",
                    f"{REFERENCE_FIELD} left blank — no placeholder written.",
                ]),
                "Shopify: Gateway Reference Empty",
            )
            return ""

        # ── Write ────────────────────────────────────────────────────────────
        values = {REFERENCE_FIELD: reference}

        gateway_name = extract_gateway_name(txn, order)
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
            f"{pe_name} (order {order_id})."
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


def _settings_for_payment_entry(pe_name: str):
    """
    Shopify Settings for the store that produced a Payment Entry.

    Resolved through the PE's Sales Order reference — Payment Entry itself
    carries no Shopify store field.
    """
    row = frappe.db.sql(
        """
        SELECT so.shopify_store
        FROM `tabPayment Entry Reference` per
        JOIN `tabSales Order` so ON so.name = per.reference_name
        WHERE per.parent = %(pe)s
          AND per.reference_doctype = 'Sales Order'
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

def _pending_payment_entries(store: str = None, limit: int = 200) -> list:
    """
    Shopify-created Payment Entries with no gateway reference yet, oldest first.

    "Shopify-created" is established by walking the PE's reference rows to a
    Sales Order that carries a shopify_order_id — Payment Entry has no Shopify
    field of its own.  Cancelled PEs (docstatus 2) are excluded.

    Grouped by PE: an order-per-PE is the norm, and a PE spanning two Shopify
    orders is pathological, so MIN() picks one deterministically.
    """
    conditions = ""
    params = {"limit": int(limit)}
    if store:
        conditions = " AND so.shopify_store = %(store)s"
        params["store"] = store

    return frappe.db.sql(
        f"""
        SELECT
            pe.name                  AS pe_name,
            MIN(so.shopify_order_id) AS shopify_order_id,
            MIN(so.shopify_store)    AS shopify_store
        FROM `tabPayment Entry` pe
        JOIN `tabPayment Entry Reference` per
              ON per.parent = pe.name
             AND per.reference_doctype = 'Sales Order'
        JOIN `tabSales Order` so
              ON so.name = per.reference_name
        WHERE pe.docstatus != 2
          AND IFNULL(pe.{REFERENCE_FIELD}, '') = ''
          AND IFNULL(so.shopify_order_id, '') != ''
          {conditions}
        GROUP BY pe.name, pe.creation
        ORDER BY pe.creation ASC
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )


@frappe.whitelist()
def backfill_gateway_references(store: str = None, limit: int = 200, dry_run: int = 0) -> dict:
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

    :param store:   shop_domain to restrict to; all stores when omitted
    :param limit:   maximum Payment Entries to process this run
    :param dry_run: when truthy, report what would be processed and write nothing
    :return: {"scanned", "updated", "no_reference", "failed", "dry_run", "entries"}
    """
    limit   = cint(limit) or 200
    dry_run = bool(cint(dry_run))

    result = {
        "scanned": 0, "updated": 0, "no_reference": 0, "failed": 0,
        "dry_run": dry_run, "entries": [],
    }

    if not _pe_has_field(REFERENCE_FIELD):
        frappe.log_error(
            f"Backfill aborted: Payment Entry has no '{REFERENCE_FIELD}' field. "
            f"Run `bench --site <site> migrate` first.",
            "Shopify: Gateway Reference Backfill Aborted",
        )
        result["aborted"] = f"Payment Entry has no '{REFERENCE_FIELD}' field."
        return result

    rows = _pending_payment_entries(store=store, limit=limit)
    result["scanned"] = len(rows)

    # One Settings doc per store, not per Payment Entry.
    settings_cache = {}

    for row in rows:
        pe_name  = row["pe_name"]
        order_id = row["shopify_order_id"]
        domain   = row.get("shopify_store") or ""

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
def enqueue_backfill(store: str = None, limit: int = 200) -> str:
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
        job_name=f"shopify_gateway_reference_backfill_{store or 'all'}",
    )
    return f"Gateway reference backfill queued for up to {limit} Payment Entries."
