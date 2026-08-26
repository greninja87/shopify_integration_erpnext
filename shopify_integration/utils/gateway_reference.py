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
    * `note_attributes` is empty for PayU orders

So it has to be pulled:

    GET /admin/api/{version}/orders/{shopify_order_id}/transactions.json

Verified against a live store: Shopify's "Payment Reference" for PayU equals
PayU's own txnid exactly — order #6428 → rkdkuLhOZPiHLp9XVygf0ASij (25 chars).

Where the value lands
---------------------
    Payment Entry.custom_gateway_reference   the gateway transaction id
    Payment Entry.custom_gateway_name        e.g. "Cards, UPI, NB by PayU India"

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


def extract_gateway_reference(txn) -> str:
    """
    The gateway reference from one transaction, or "".

    Priority: authorization → receipt.txnid → receipt.payment_id.
    Returns "" rather than a placeholder when every source is empty.
    """
    if not isinstance(txn, dict):
        return ""

    for path in _REFERENCE_PATHS:
        if len(path) == 1:
            candidate = _clean(txn.get(path[0]))
        else:
            candidate = _clean(_receipt(txn).get(path[1]))
        if candidate:
            return candidate[:_MAX_REFERENCE_LEN]

    return ""


def extract_gateway_name(txn) -> str:
    """The gateway that processed the transaction, e.g. 'Cards, UPI, NB by PayU India'."""
    if not isinstance(txn, dict):
        return ""
    return _clean(txn.get("gateway"))[:_MAX_REFERENCE_LEN]


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
        if not txn:
            frappe.log_error(
                f"No successful sale/capture transaction on Shopify order {order_id} "
                f"(Payment Entry {pe_name}). {len(transactions or [])} transaction(s) "
                f"returned. Gateway reference left blank.",
                "Shopify: Gateway Reference Not Found",
            )
            return ""

        reference = extract_gateway_reference(txn)
        if not reference:
            frappe.log_error(
                "\n".join([
                    f"Shopify order {order_id} (Payment Entry {pe_name}): transaction "
                    f"{txn.get('id')} carries no gateway reference.",
                    "",
                    f"gateway            : {txn.get('gateway') or '(blank)'}",
                    f"authorization      : {txn.get('authorization') or '(blank)'}",
                    f"receipt keys       : {sorted(_receipt(txn).keys()) or '(none)'}",
                    "",
                    f"{REFERENCE_FIELD} left blank — no placeholder written.",
                ]),
                "Shopify: Gateway Reference Empty",
            )
            return ""

        # ── Write ────────────────────────────────────────────────────────────
        values = {REFERENCE_FIELD: reference}

        gateway_name = extract_gateway_name(txn)
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
