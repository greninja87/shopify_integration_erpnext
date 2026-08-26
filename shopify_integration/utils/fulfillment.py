"""
fulfillment.py — Fulfil Shopify orders from ERPNext Delivery Notes.

Submitting a Delivery Note is the moment ERPNext knows goods left the building,
so that is the moment Shopify should be told.  Shopify then marks the order
fulfilled and (when notify_customer is on) emails the customer their shipping
confirmation.

Three trigger modes, per store (Shopify Settings → Fulfillment):

    Manual      nothing automatic; the Delivery Note button / bulk action only
    Immediate   on_submit enqueues the fulfillment right away
    Scheduled   the hourly job fulfils Delivery Notes submitted more than
                dn_fulfillment_delay_hours ago

Scheduled exists for the cancel-and-redo case: submit the wrong Delivery Note,
cancel it inside the delay window, and Shopify never hears about it.

The API shape
-------------
Fulfillment is fulfillment-order-shaped and GraphQL-only.  You cannot post a
fulfillment against an order; you must:

    1. query order.fulfillmentOrders
    2. keep the ones whose supportedActions include CREATE_FULFILLMENT
       (the others are assigned to a third-party service and need
        fulfillmentOrderSubmitFulfillmentRequest instead — we refuse loudly
        rather than pretend)
    3. call fulfillmentCreate with lineItemsByFulfillmentOrder

Line matching
-------------
Delivery Note item → so_detail → Sales Order Item →
custom_shopify_line_item_id → FulfillmentOrderLineItem.lineItem.id

Matching on the Shopify line item id (not SKU) matters: one order can carry the
same SKU on two separate line items — different discounts, different line
properties — and SKU alone cannot tell them apart.  Orders synced before that
field existed fall back to SKU matching, which is correct for the overwhelmingly
common one-line-per-SKU case.

Idempotency
-----------
custom_shopify_fulfillment_id being set is a hard stop on every path.  Four
things can trigger a fulfillment (on_submit, scheduler, form button, bulk
action) and they can race, so the claim is a compare-and-swap on the status
field, committed before any HTTP happens.  A claim left stale by a killed worker
is reclaimed by the scheduler after STALE_CLAIM_MINUTES.

Failure policy
--------------
A fulfillment failure must never block or unwind a Delivery Note.  The DN is a
stock document and Shopify is downstream of it.  Every failure path records
status Failed with the reason, alerts, and leaves the document alone — the
scheduler and the retry button both re-pick Failed rows.
"""

import json

import frappe
from frappe.utils import add_to_date, cint, flt, now_datetime

from shopify_integration.utils.shopify_api import ShopifyAPIError, has_admin_api_credentials
from shopify_integration.utils.shopify_graphql import (
    ShopifyUserError,
    check_user_errors,
    execute,
    gid,
)

# Delivery Note state fields
FULFILLMENT_ID_FIELD     = "custom_shopify_fulfillment_id"
FULFILLMENT_STATUS_FIELD = "custom_shopify_fulfillment_status"
FULFILLED_AT_FIELD       = "custom_shopify_fulfilled_at"
FULFILLMENT_ERROR_FIELD  = "custom_shopify_fulfillment_error"

# Sales Order Item field added for line matching
LINE_ITEM_ID_FIELD = "custom_shopify_line_item_id"

STATUS_PENDING    = "Pending"
STATUS_FULFILLED  = "Fulfilled"
STATUS_PARTIAL    = "Partially Fulfilled"
STATUS_FAILED     = "Failed"
STATUS_CANCELLED  = "Cancelled"
STATUS_NA         = "Not Applicable"

# A fulfillment order can only be acted on while work hasn't finished.
_ACTIONABLE_FO_STATUSES = ("OPEN", "IN_PROGRESS", "SCHEDULED", "ON_HOLD")

# supportedActions value that means "this app may create the fulfillment itself".
_ACTION_CREATE  = "CREATE_FULFILLMENT"
_ACTION_REQUEST = "REQUEST_FULFILLMENT"

# A Pending claim older than this is assumed abandoned (worker killed
# mid-request) and becomes eligible again.
STALE_CLAIM_MINUTES = 30

# Shopify caps fulfillmentOrderLineItems at 512 per fulfillment order.
_MAX_LINE_ITEMS = 512


# ── GraphQL documents ─────────────────────────────────────────────────────────

_FULFILLMENT_ORDERS_QUERY = """
query orderFulfillmentOrders($id: ID!) {
  order(id: $id) {
    id
    name
    displayFulfillmentStatus
    fulfillmentOrders(first: 25) {
      pageInfo { hasNextPage }
      nodes {
        id
        status
        requestStatus
        supportedActions { action }
        assignedLocation { name }
        lineItems(first: 250) {
          pageInfo { hasNextPage }
          nodes {
            id
            remainingQuantity
            totalQuantity
            sku
            lineItem { id sku }
          }
        }
      }
    }
  }
}
"""

_FULFILLMENT_CREATE_MUTATION = """
mutation shopifyFulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment {
      id
      status
      createdAt
      trackingInfo { company number url }
    }
    userErrors { field message }
  }
}
"""

_FULFILLMENT_CANCEL_MUTATION = """
mutation shopifyFulfillmentCancel($id: ID!) {
  fulfillmentCancel(id: $id) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""


# ── Pure logic (unit-tested) ──────────────────────────────────────────────────

def classify_fulfillment_orders(nodes) -> dict:
    """
    Split an order's fulfillment orders by what we're allowed to do with them.

    Returns {"creatable": [...], "third_party": [...], "inactive": [...]}.

    supportedActions is the authority here, not the status: a fulfillment order
    assigned to a 3PL is OPEN but only offers REQUEST_FULFILLMENT.  Guessing
    from the status would have us call the wrong mutation.
    """
    result = {"creatable": [], "third_party": [], "inactive": []}

    for node in (nodes or []):
        if not isinstance(node, dict):
            continue

        status = (node.get("status") or "").upper()
        actions = {
            (a or {}).get("action")
            for a in (node.get("supportedActions") or [])
            if isinstance(a, dict)
        }

        if status not in _ACTIONABLE_FO_STATUSES:
            result["inactive"].append(node)
        elif _ACTION_CREATE in actions:
            result["creatable"].append(node)
        elif _ACTION_REQUEST in actions:
            result["third_party"].append(node)
        else:
            result["inactive"].append(node)

    return result


def _fo_line_key(fo_line) -> tuple:
    """(shopify_line_item_id, sku) for one FulfillmentOrderLineItem node."""
    line_item = fo_line.get("lineItem") or {}
    return (
        str(line_item.get("id") or "").strip(),
        (fo_line.get("sku") or line_item.get("sku") or "").strip().lower(),
    )


def plan_fulfillment(fulfillment_orders, wanted) -> dict:
    """
    Work out exactly which fulfillment order line items to fulfil, and how many.

    :param fulfillment_orders: nodes from order.fulfillmentOrders
    :param wanted: [{"line_item_id": str, "sku": str, "qty": int}] from the
                   Delivery Note
    :return: {
        "line_items_by_fulfillment_order": payload for FulfillmentInput,
        "allocated": total units allocated,
        "unallocated": [{"sku", "qty", "reason"}],
        "third_party": [fulfillment order ids needing a fulfillment REQUEST],
        "locations": [assigned location names touched],
      }

    Allocation rules:
      * a wanted line matches a fulfillment order line item by Shopify line
        item id first, by SKU only as a fallback
      * never allocate more than remainingQuantity — another Delivery Note may
        already have covered part of it
      * a line already fully fulfilled in Shopify is reported as unallocated
        with reason "already fulfilled", which is not an error
    """
    classified = classify_fulfillment_orders(fulfillment_orders)

    # Flatten creatable fulfillment order line items into a mutable pool.
    pool = []
    locations = []
    for fo in classified["creatable"]:
        location = ((fo.get("assignedLocation") or {}).get("name") or "").strip()
        if location and location not in locations:
            locations.append(location)
        for fo_line in ((fo.get("lineItems") or {}).get("nodes") or []):
            if not isinstance(fo_line, dict):
                continue
            line_item_id, sku = _fo_line_key(fo_line)
            pool.append({
                "fo_id": fo.get("id"),
                "fo_line_id": fo_line.get("id"),
                "line_item_id": line_item_id,
                "sku": sku,
                "remaining": cint(fo_line.get("remainingQuantity")),
            })

    # fulfillment order id -> [{id, quantity}]
    grouped = {}
    unallocated = []
    total_allocated = 0

    for want in (wanted or []):
        qty = cint(want.get("qty"))
        sku = (want.get("sku") or "").strip()
        line_item_id = str(want.get("line_item_id") or "").strip()

        if qty <= 0:
            continue

        # Exact line-item-id match first; SKU only when we have no id to go on.
        if line_item_id:
            candidates = [p for p in pool if p["line_item_id"] == line_item_id]
        else:
            candidates = [p for p in pool if p["sku"] and p["sku"] == sku.lower()]

        if not candidates:
            unallocated.append({
                "sku": sku, "qty": qty,
                "reason": "no matching open fulfillment order line",
            })
            continue

        remaining_to_place = qty
        for candidate in candidates:
            if remaining_to_place <= 0:
                break
            take = min(candidate["remaining"], remaining_to_place)
            if take <= 0:
                continue
            rows = grouped.setdefault(candidate["fo_id"], [])
            rows.append({"id": candidate["fo_line_id"], "quantity": take})
            candidate["remaining"] -= take
            remaining_to_place -= take
            total_allocated += take

        if remaining_to_place > 0:
            unallocated.append({
                "sku": sku, "qty": remaining_to_place,
                "reason": "already fulfilled in Shopify, or no quantity remaining",
            })

    line_items_by_fo = [
        {"fulfillmentOrderId": fo_id, "fulfillmentOrderLineItems": rows[:_MAX_LINE_ITEMS]}
        for fo_id, rows in grouped.items()
        if rows
    ]

    return {
        "line_items_by_fulfillment_order": line_items_by_fo,
        "allocated": total_allocated,
        "unallocated": unallocated,
        "third_party": [fo.get("id") for fo in classified["third_party"]],
        "locations": locations,
    }


def build_tracking_info(number: str = "", company: str = "", url: str = ""):
    """
    FulfillmentTrackingInput, or None when there is nothing to send.

    Shopify auto-builds the tracking URL when `company` exactly matches a name
    from its supported-carriers list (capitalisation included).  When it does
    not, the URL has to be supplied or the number renders as plain text.
    """
    number  = (number or "").strip()
    company = (company or "").strip()
    url     = (url or "").strip()

    if not (number or company or url):
        return None

    info = {}
    if number:
        info["number"] = number
    if company:
        info["company"] = company
    if url:
        info["url"] = url
    return info


def build_fulfillment_input(plan: dict, notify_customer: bool, tracking_info=None) -> dict:
    """Assemble FulfillmentInput from a plan."""
    payload = {
        "lineItemsByFulfillmentOrder": plan["line_items_by_fulfillment_order"],
        "notifyCustomer": bool(notify_customer),
    }
    if tracking_info:
        payload["trackingInfo"] = tracking_info
    return payload


# ── Delivery Note reading ─────────────────────────────────────────────────────

def _dn_has_state_fields() -> bool:
    try:
        meta = frappe.get_meta("Delivery Note")
        return bool(meta.has_field(FULFILLMENT_ID_FIELD)) and bool(
            meta.has_field(FULFILLMENT_STATUS_FIELD)
        )
    except Exception:
        return False


def _linked_shopify_order(dn_name: str):
    """
    (shopify_order_id, shopify_store) for a Delivery Note, or (None, None).

    Resolved through the DN items' against_sales_order rather than the DN header
    — the same reasoning as create_si_from_dn_on_submit: the header fields are
    not reliably populated on every DN.
    """
    row = frappe.db.sql(
        """
        SELECT so.shopify_order_id, so.shopify_store
        FROM `tabDelivery Note Item` dni
        JOIN `tabSales Order` so ON so.name = dni.against_sales_order
        WHERE dni.parent = %(dn)s
          AND IFNULL(so.shopify_order_id, '') != ''
        LIMIT 1
        """,
        {"dn": dn_name},
        as_dict=True,
    )
    if not row:
        return None, None
    return row[0]["shopify_order_id"], row[0]["shopify_store"]


def wanted_lines_for_dn(dn_name: str) -> list:
    """
    What this Delivery Note claims to have shipped, as
    [{"line_item_id", "sku", "qty"}].

    Quantities are floored to integers because Shopify fulfilment quantities are
    integers.  A fractional DN quantity therefore under-fulfils rather than
    failing outright, and the shortfall surfaces in the plan's `unallocated`.
    """
    rows = frappe.db.sql(
        """
        SELECT
            dni.item_code,
            dni.qty,
            soi.{line_field} AS line_item_id
        FROM `tabDelivery Note Item` dni
        LEFT JOIN `tabSales Order Item` soi ON soi.name = dni.so_detail
        WHERE dni.parent = %(dn)s
        """.format(line_field=LINE_ITEM_ID_FIELD),
        {"dn": dn_name},
        as_dict=True,
    )

    # Aggregate by (line item id, sku): a DN can legitimately carry two rows
    # against the same Sales Order line.
    merged = {}
    for row in rows:
        line_item_id = str(row.get("line_item_id") or "").strip()
        sku = (row.get("item_code") or "").strip()
        qty = int(flt(row.get("qty")))   # floor
        if qty <= 0:
            continue
        key = (line_item_id, sku.lower())
        if key in merged:
            merged[key]["qty"] += qty
        else:
            merged[key] = {"line_item_id": line_item_id, "sku": sku, "qty": qty}

    return list(merged.values())


def _tracking_for_dn(dn_name: str, settings) -> dict:
    """
    Read tracking details off the Delivery Note using the field names configured
    in Shopify Settings.

    Configurable rather than hardcoded because every install keeps its tracking
    id somewhere different — a custom field, lr_no, a courier integration's own
    field.  Same approach as gst_field_path.
    """
    number_field  = (settings.get("dn_tracking_number_field") or "").strip()
    company_field = (settings.get("dn_tracking_company_field") or "").strip()
    url_field     = (settings.get("dn_tracking_url_field") or "").strip()

    fieldnames = [f for f in (number_field, company_field, url_field) if f]
    if not fieldnames:
        return {"number": "", "company": "", "url": ""}

    meta = frappe.get_meta("Delivery Note")
    valid = [f for f in fieldnames if meta.has_field(f)]

    missing = sorted(set(fieldnames) - set(valid))
    if missing:
        frappe.log_error(
            f"Delivery Note has no field(s) {', '.join(missing)} — configured in "
            f"Shopify Settings → Fulfillment as tracking source. Tracking info "
            f"for {dn_name} will be incomplete. Fix the field name(s) in Settings.",
            "Shopify: Fulfillment Tracking Field Missing",
        )

    values = {}
    if valid:
        fetched = frappe.db.get_value("Delivery Note", dn_name, valid, as_dict=True) or {}
        values = dict(fetched)

    return {
        "number":  str(values.get(number_field) or "").strip() if number_field else "",
        "company": str(values.get(company_field) or "").strip() if company_field else "",
        "url":     str(values.get(url_field) or "").strip() if url_field else "",
    }


# ── Claim / state ─────────────────────────────────────────────────────────────

def _set_state(dn_name: str, **values):
    """Write fulfillment state without touching `modified` or making Versions."""
    if not values:
        return
    frappe.db.set_value("Delivery Note", dn_name, values, update_modified=False)


def _claim(dn_name: str) -> bool:
    """
    Claim a Delivery Note for fulfillment, or return False if someone else has.

    on_submit, the scheduler, the form button and the bulk action can all fire
    at the same document.  A read-then-write check would let two of them both
    see "not fulfilled" and create two fulfillments in Shopify, so the claim is
    a compare-and-swap under a row lock, committed before any HTTP happens.

    A Pending claim older than STALE_CLAIM_MINUTES is treated as abandoned — a
    worker killed mid-request must not block the document forever.
    """
    # Short row lock: read-modify-write only, no network inside it.
    frappe.db.sql(
        "SELECT name FROM `tabDelivery Note` WHERE name = %(dn)s FOR UPDATE",
        {"dn": dn_name},
    )

    current = frappe.db.get_value(
        "Delivery Note", dn_name,
        [FULFILLMENT_ID_FIELD, FULFILLMENT_STATUS_FIELD],
        as_dict=True,
    ) or {}

    if (current.get(FULFILLMENT_ID_FIELD) or "").strip():
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — release the row lock
        return False

    if (current.get(FULFILLMENT_STATUS_FIELD) or "") == STATUS_PENDING:
        claimed_at = _claim_timestamp(dn_name)
        cutoff = add_to_date(now_datetime(), minutes=-STALE_CLAIM_MINUTES)
        if claimed_at and claimed_at > cutoff:
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — release the row lock
            return False

    # Status and stamp in ONE write.  Writing the stamp separately would leave a
    # window where another worker sees status=Pending with a stale-or-absent
    # timestamp, judges the claim abandoned, and claims it as well.
    _set_state(dn_name, **{
        FULFILLMENT_STATUS_FIELD: STATUS_PENDING,
        FULFILLED_AT_FIELD: now_datetime(),
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — claim must be visible to other workers before we call Shopify
    return True


def _claim_timestamp(dn_name: str):
    """
    When the current Pending claim was taken.

    State writes use update_modified=False, so `modified` cannot date the claim.
    custom_shopify_fulfilled_at doubles as the claim stamp: written when the
    claim is taken, overwritten with the real fulfilment time on success.  On a
    Failed row it therefore reads as "last attempted at", which is what the
    field description says.
    """
    return frappe.db.get_value("Delivery Note", dn_name, FULFILLED_AT_FIELD)


def _release_claim(dn_name: str, status: str, error: str = ""):
    _set_state(dn_name, **{
        FULFILLMENT_STATUS_FIELD: status,
        FULFILLMENT_ERROR_FIELD: (error or "")[:1000],
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; state must persist


# ── Eligibility ───────────────────────────────────────────────────────────────

def _settings_for_store(shop_domain: str, require_enabled: bool = True):
    filters = {"shop_domain": shop_domain, "enable_sync": 1}
    if require_enabled:
        filters["enable_fulfillment"] = 1
    name = frappe.db.get_value("Shopify Settings", filters, "name")
    return frappe.get_doc("Shopify Settings", name) if name else None


def check_eligibility(dn_name: str, settings=None) -> dict:
    """
    Can this Delivery Note be fulfilled in Shopify right now?

    Returns {"ok": bool, "reason": str, "settings": doc|None,
             "shopify_order_id": str, "status": current status}.
    Read-only — safe to call from the client on every form refresh.
    """
    out = {"ok": False, "reason": "", "settings": None,
           "shopify_order_id": "", "status": ""}

    if not _dn_has_state_fields():
        out["reason"] = (
            "Delivery Note is missing the Shopify fulfillment fields. "
            "Run `bench --site <site> migrate`."
        )
        return out

    dn = frappe.db.get_value(
        "Delivery Note", dn_name,
        ["docstatus", "is_return", FULFILLMENT_ID_FIELD, FULFILLMENT_STATUS_FIELD],
        as_dict=True,
    )
    if not dn:
        out["reason"] = "Delivery Note not found."
        return out

    out["status"] = dn.get(FULFILLMENT_STATUS_FIELD) or ""

    if (dn.get(FULFILLMENT_ID_FIELD) or "").strip():
        out["reason"] = "Already fulfilled in Shopify."
        return out

    if cint(dn.get("is_return")):
        out["reason"] = "Return Delivery Notes are handled as Credit Notes, not fulfillments."
        return out

    if cint(dn.get("docstatus")) != 1:
        out["reason"] = "Delivery Note must be submitted."
        return out

    shopify_order_id, shop_domain = _linked_shopify_order(dn_name)
    if not shopify_order_id:
        out["reason"] = "Not linked to a Shopify Sales Order."
        return out

    out["shopify_order_id"] = shopify_order_id

    settings = settings or _settings_for_store(shop_domain)
    if not settings:
        out["reason"] = (
            f"Fulfillment is not enabled for store '{shop_domain}' "
            f"(Shopify Settings → Fulfillment)."
        )
        return out

    if not has_admin_api_credentials(settings):
        out["reason"] = (
            "No Admin API access token configured for this store "
            "(Shopify Settings → Connection → Shopify Admin API)."
        )
        return out

    out["settings"] = settings
    out["ok"] = True
    return out


# ── Main entry point ──────────────────────────────────────────────────────────

def fulfil_delivery_note(dn_name: str, settings=None, triggered_by: str = "manual") -> dict:
    """
    Fulfil one Delivery Note's order in Shopify.

    Idempotent and safe to call from anywhere: on_submit, the scheduler, the
    form button, the bulk action.  Never raises — the Delivery Note is a stock
    document and must not be affected by a downstream API problem.

    :return: {"ok", "status", "fulfillment_id", "message"}
    """
    def result(ok, status, message, fulfillment_id=""):
        return {"ok": ok, "status": status, "message": message,
                "fulfillment_id": fulfillment_id, "delivery_note": dn_name}

    previous_status = ""
    try:
        eligibility = check_eligibility(dn_name, settings=settings)
        if not eligibility["ok"]:
            # Not an error — most of these are "nothing to do here".
            return result(False, eligibility["status"], eligibility["reason"])

        settings = eligibility["settings"]
        shopify_order_id = eligibility["shopify_order_id"]
        # Remembered so a permanent failure (e.g. a 3PL-assigned order) alerts
        # once rather than on every hourly scheduler retry.
        previous_status = eligibility["status"]

        if not _claim(dn_name):
            return result(False, STATUS_PENDING,
                          "Another process is already fulfilling this Delivery Note.")

    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Shopify: Fulfillment Setup Failed — {dn_name}")
        return result(False, "", "Could not start fulfillment; see the Error Log.")

    # Alerting on repeats is noise, not signal: a permanently unfulfillable
    # order (3PL-assigned, deleted in Shopify) is re-picked by the hourly
    # scheduler forever, and mailing the admin every hour trains them to ignore
    # the alert.  First failure alerts; retries log only.
    def fail(message, alert=True):
        _release_claim(dn_name, STATUS_FAILED, message)
        if alert and previous_status != STATUS_FAILED:
            _alert(settings, dn_name, message)
        return result(False, STATUS_FAILED, message)

    # ── Everything past the claim must land in a definite state ──────────────
    try:
        wanted = wanted_lines_for_dn(dn_name)
        if not wanted:
            return fail("No Delivery Note lines with a positive integer quantity.")

        data = execute(
            settings,
            _FULFILLMENT_ORDERS_QUERY,
            {"id": gid("Order", shopify_order_id)},
            operation="orderFulfillmentOrders",
        )

        order = (data or {}).get("order")
        if not order:
            return fail(
                f"Shopify order {shopify_order_id} not found — it may have been "
                f"deleted, or the token cannot see it."
            )

        fulfillment_orders = order.get("fulfillmentOrders") or {}
        fo_nodes = fulfillment_orders.get("nodes") or []

        # Never let a page limit look like "there was nothing more".  An order
        # past these bounds is extraordinary, but silently fulfilling part of it
        # and reporting success would be worse than saying so.
        _warn_if_truncated(dn_name, shopify_order_id, fulfillment_orders, fo_nodes)

        plan = plan_fulfillment(fo_nodes, wanted)

        # ── Nothing we can create ────────────────────────────────────────────
        if plan["allocated"] <= 0:
            if plan["third_party"]:
                msg = (
                    f"Shopify order {order.get('name') or shopify_order_id} is assigned "
                    f"to a third-party fulfillment service. Those require a fulfillment "
                    f"REQUEST (fulfillmentOrderSubmitFulfillmentRequest), which this app "
                    f"does not send. Fulfil it in Shopify admin instead."
                )
                return fail(msg)

            # Everything already fulfilled in Shopify — someone did it there.
            # Not a failure: record the truth and stop retrying.
            already = all(
                "already fulfilled" in (u.get("reason") or "")
                for u in plan["unallocated"]
            ) and bool(plan["unallocated"])
            if already or (order.get("displayFulfillmentStatus") or "").upper() == "FULFILLED":
                msg = ("Shopify already shows this order as fulfilled — nothing sent. "
                       "No ERPNext-created fulfillment exists for it.")
                _release_claim(dn_name, STATUS_FULFILLED, msg)
                return result(True, STATUS_FULFILLED, msg)

            return fail(
                "No open Shopify fulfillment order line matched this Delivery Note: "
                + json.dumps(plan["unallocated"])[:500]
            )

        # ── Create the fulfillment ───────────────────────────────────────────
        tracking = _tracking_for_dn(dn_name, settings)
        tracking_info = build_tracking_info(
            number=tracking["number"],
            company=tracking["company"] or (settings.get("default_tracking_company") or ""),
            url=tracking["url"],
        )
        notify = cint(settings.get("notify_customer_on_fulfillment"))

        payload = execute(
            settings,
            _FULFILLMENT_CREATE_MUTATION,
            {"fulfillment": build_fulfillment_input(plan, notify, tracking_info)},
            operation="fulfillmentCreate",
        )
        created = check_user_errors(payload, "fulfillmentCreate", context=dn_name)

        fulfillment = created.get("fulfillment") or {}
        fulfillment_id = str(fulfillment.get("id") or "").strip()
        if not fulfillment_id:
            return fail("fulfillmentCreate returned no fulfillment id.")

        partial = bool(plan["unallocated"])
        status = STATUS_PARTIAL if partial else STATUS_FULFILLED
        note = ""
        if partial:
            note = ("Fulfilled, but some quantity was not sent: "
                    + json.dumps(plan["unallocated"])[:800])

        _set_state(dn_name, **{
            FULFILLMENT_ID_FIELD: fulfillment_id,
            FULFILLMENT_STATUS_FIELD: status,
            FULFILLED_AT_FIELD: now_datetime(),
            FULFILLMENT_ERROR_FIELD: note[:1000],
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; fulfillment id must persist

        frappe.logger().info(
            f"Shopify: fulfilled order {shopify_order_id} from {dn_name} "
            f"({plan['allocated']} unit(s), notify={bool(notify)}, "
            f"trigger={triggered_by}) → {fulfillment_id}"
        )
        return result(True, status, note or "Fulfilled in Shopify.", fulfillment_id)

    except ShopifyUserError as exc:
        msg = str(exc)
        _release_claim(dn_name, STATUS_FAILED, msg)
        frappe.log_error(
            f"{msg}\n\nuserErrors: {json.dumps(exc.user_errors)[:1000]}",
            f"Shopify: Fulfillment Rejected — {dn_name}",
        )
        _alert(settings, dn_name, msg)
        return result(False, STATUS_FAILED, msg)

    except ShopifyAPIError as exc:
        msg = str(exc)
        _release_claim(dn_name, STATUS_FAILED, msg)
        frappe.log_error(msg, f"Shopify: Fulfillment API Error — {dn_name}")
        _alert(settings, dn_name, msg)
        return result(False, STATUS_FAILED, msg)

    except Exception:
        tb = frappe.get_traceback()
        _release_claim(dn_name, STATUS_FAILED, "Unexpected error; see the Error Log.")
        frappe.log_error(tb, f"Shopify: Fulfillment Failed — {dn_name}")
        _alert(settings, dn_name, tb)
        return result(False, STATUS_FAILED, "Unexpected error; see the Error Log.")


def _warn_if_truncated(dn_name, shopify_order_id, fulfillment_orders, fo_nodes):
    """
    Log when the order has more fulfillment orders or line items than one page
    of the query returns.

    The plan built from a truncated page is still correct for what it saw, but
    it is not the whole order — so this must be visible rather than looking like
    a clean partial fulfillment.
    """
    if ((fulfillment_orders.get("pageInfo") or {}).get("hasNextPage")):
        frappe.log_error(
            f"Shopify order {shopify_order_id} has more than 25 fulfillment orders. "
            f"{dn_name} was planned against the first page only — some lines may be "
            f"left unfulfilled. Fulfil the remainder in Shopify admin.",
            "Shopify: Fulfillment Orders Truncated",
        )

    for fo in fo_nodes:
        line_items = (fo or {}).get("lineItems") or {}
        if (line_items.get("pageInfo") or {}).get("hasNextPage"):
            frappe.log_error(
                f"Shopify fulfillment order {fo.get('id')} on order "
                f"{shopify_order_id} has more than 250 line items. {dn_name} was "
                f"planned against the first page only — some lines may be left "
                f"unfulfilled.",
                "Shopify: Fulfillment Order Lines Truncated",
            )


def _alert(settings, dn_name: str, message: str):
    """Email the configured failure recipients.  Never raises."""
    try:
        from shopify_integration.utils.sales_invoice import _send_si_failure_email

        _send_si_failure_email(settings, "Delivery Note", dn_name, message)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Fulfillment Alert Failed — {dn_name}"
        )


# ── Cancellation ──────────────────────────────────────────────────────────────

def cancel_fulfillment_for_dn(dn_name: str, store_name: str = "") -> dict:
    """
    Cancel the Shopify fulfillment created from a Delivery Note.

    Runs after the Delivery Note has already been cancelled in ERPNext, so it
    can only report — never block.  When Shopify refuses (a shipped, notified
    fulfillment is not always reversible) that is alerted loudly rather than
    swallowed: the two systems now disagree and a human has to settle it.
    """
    try:
        fulfillment_id = (
            frappe.db.get_value("Delivery Note", dn_name, FULFILLMENT_ID_FIELD) or ""
        ).strip()
        if not fulfillment_id:
            return {"ok": True, "message": "Nothing to cancel."}

        settings = (
            frappe.get_doc("Shopify Settings", store_name) if store_name else None
        )
        if not settings:
            _, shop_domain = _linked_shopify_order(dn_name)
            settings = _settings_for_store(shop_domain, require_enabled=False)
        if not settings or not has_admin_api_credentials(settings):
            msg = (f"Cannot cancel Shopify fulfillment {fulfillment_id} for {dn_name}: "
                   f"store settings or Admin API token unavailable. Shopify still shows "
                   f"this order as fulfilled.")
            frappe.log_error(msg, f"Shopify: Fulfillment Cancel Skipped — {dn_name}")
            return {"ok": False, "message": msg}

        data = execute(
            settings,
            _FULFILLMENT_CANCEL_MUTATION,
            {"id": fulfillment_id},
            operation="fulfillmentCancel",
        )
        check_user_errors(data, "fulfillmentCancel", context=dn_name)

        _set_state(dn_name, **{
            FULFILLMENT_STATUS_FIELD: STATUS_CANCELLED,
            FULFILLMENT_ERROR_FIELD: "",
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; state must persist

        frappe.logger().info(
            f"Shopify: cancelled fulfillment {fulfillment_id} for {dn_name}"
        )
        return {"ok": True, "message": f"Cancelled fulfillment {fulfillment_id}."}

    except Exception as exc:
        msg = (
            f"Failed to cancel Shopify fulfillment for {dn_name}: {exc}\n\n"
            f"The Delivery Note is cancelled in ERPNext but Shopify still shows the "
            f"order as FULFILLED. Reverse it in Shopify admin, or accept the "
            f"divergence deliberately."
        )
        frappe.log_error(msg, f"Shopify: Fulfillment Cancel Failed — {dn_name}")
        try:
            _set_state(dn_name, **{FULFILLMENT_ERROR_FIELD: msg[:1000]})
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — record the divergence
        except Exception:
            pass
        return {"ok": False, "message": msg}


# ── Document hooks ────────────────────────────────────────────────────────────

def fulfil_on_dn_submit(doc, method=None):
    """
    Delivery Note on_submit.  Acts only when dn_fulfillment_timing == "Immediate".

    Enqueued with enqueue_after_commit=True so the submit transaction lands
    before the job runs — the same pattern as create_si_from_dn_on_submit.
    """
    try:
        if doc.get("is_return"):
            return

        so_name = next(
            (i.against_sales_order for i in (doc.get("items") or [])
             if i.get("against_sales_order")),
            None,
        )
        if not so_name:
            return

        shopify_store = frappe.db.get_value("Sales Order", so_name, "shopify_store")
        if not shopify_store:
            return

        settings_name = frappe.db.get_value(
            "Shopify Settings",
            {
                "shop_domain": shopify_store,
                "enable_sync": 1,
                "enable_fulfillment": 1,
                "dn_fulfillment_timing": "Immediate",
            },
            "name",
        )
        if not settings_name:
            return

        frappe.enqueue(
            "shopify_integration.utils.fulfillment._fulfil_background",
            queue="short",
            timeout=300,
            dn_name=doc.name,
            store_name=settings_name,
            triggered_by="on_submit",
            job_name=f"shopify_fulfil_{doc.name}",
            enqueue_after_commit=True,
        )
    except Exception:
        # A hook must never break the submit it is attached to.
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Fulfillment Enqueue Failed — {doc.name}"
        )


def handle_dn_cancel(doc, method=None):
    """
    Delivery Note on_cancel.

    Honours Shopify Settings → Fulfillment → On Delivery Note Cancel:
        "Do Nothing"                      leave Shopify fulfilled, log it
        "Cancel Fulfillment in Shopify"   call fulfillmentCancel

    Enqueued after commit so a Shopify problem can never block or roll back the
    cancellation of a stock document.
    """
    try:
        fulfillment_id = (doc.get(FULFILLMENT_ID_FIELD) or "").strip()
        if not fulfillment_id:
            return

        _, shop_domain = _linked_shopify_order(doc.name)
        settings = _settings_for_store(shop_domain, require_enabled=False)
        action = (settings.get("dn_cancel_action") if settings else "") or "Do Nothing"

        if action != "Cancel Fulfillment in Shopify":
            frappe.log_error(
                f"Delivery Note {doc.name} was cancelled but Shopify fulfillment "
                f"{fulfillment_id} is left standing — Shopify Settings → Fulfillment "
                f"→ On Delivery Note Cancel is set to '{action}'. Shopify still shows "
                f"this order as fulfilled.",
                "Shopify: Fulfillment Left Standing After DN Cancel",
            )
            return

        frappe.enqueue(
            "shopify_integration.utils.fulfillment.cancel_fulfillment_for_dn",
            queue="short",
            timeout=300,
            dn_name=doc.name,
            store_name=settings.name if settings else "",
            job_name=f"shopify_unfulfil_{doc.name}",
            enqueue_after_commit=True,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Fulfillment Cancel Enqueue Failed — {doc.name}"
        )


def _fulfil_background(dn_name: str, store_name: str = "", triggered_by: str = "scheduled"):
    """Background worker wrapper."""
    settings = frappe.get_doc("Shopify Settings", store_name) if store_name else None
    fulfil_delivery_note(dn_name, settings=settings, triggered_by=triggered_by)


# ── Whitelisted endpoints (form button / bulk action / client status) ──────────

@frappe.whitelist()
def get_dn_fulfillment_status(dn_name: str) -> dict:
    """
    Everything the Delivery Note form needs to render its fulfillment banner and
    decide whether to show the button.  Read-only.
    """
    if not _dn_has_state_fields():
        return {"is_shopify": False, "migrated": False}

    dn = frappe.db.get_value(
        "Delivery Note", dn_name,
        ["docstatus", "is_return", FULFILLMENT_ID_FIELD,
         FULFILLMENT_STATUS_FIELD, FULFILLED_AT_FIELD, FULFILLMENT_ERROR_FIELD],
        as_dict=True,
    ) or {}

    shopify_order_id, shop_domain = _linked_shopify_order(dn_name)
    if not shopify_order_id:
        return {"is_shopify": False, "migrated": True}

    settings = _settings_for_store(shop_domain)
    eligibility = check_eligibility(dn_name, settings=settings)

    return {
        "is_shopify": True,
        "migrated": True,
        "enabled": bool(settings),
        "timing": (settings.get("dn_fulfillment_timing") if settings else "") or "Manual",
        "delay_hours": cint(settings.get("dn_fulfillment_delay_hours")) if settings else 0,
        "notify_customer": bool(cint(settings.get("notify_customer_on_fulfillment"))) if settings else False,
        "fulfillment_id": dn.get(FULFILLMENT_ID_FIELD) or "",
        "status": dn.get(FULFILLMENT_STATUS_FIELD) or "",
        "fulfilled_at": dn.get(FULFILLED_AT_FIELD),
        "error": dn.get(FULFILLMENT_ERROR_FIELD) or "",
        "can_fulfil": eligibility["ok"],
        "reason": eligibility["reason"],
        "is_return": cint(dn.get("is_return")),
        "docstatus": cint(dn.get("docstatus")),
    }


@frappe.whitelist()
def fulfil_now(dn_name: str) -> dict:
    """
    Fulfil one Delivery Note on demand — the form's "Fulfil in Shopify" button.

    Runs inline rather than enqueued so the user gets the real outcome back
    instead of an optimistic "queued".  One order is one or two GraphQL calls,
    well inside a web request.
    """
    frappe.has_permission("Delivery Note", "submit", doc=dn_name, throw=True)
    return fulfil_delivery_note(dn_name, triggered_by="manual")


@frappe.whitelist()
def fulfil_bulk(dn_names) -> dict:
    """
    Fulfil several Delivery Notes — the list view's bulk action.

    Enqueued: pacing means N documents take N/2 seconds at best, which would
    time out a web request for any realistic selection.  Each document is
    independent, so one failure never stops the batch.
    """
    if isinstance(dn_names, str):
        dn_names = json.loads(dn_names)
    dn_names = [n for n in (dn_names or []) if n]

    if not dn_names:
        return {"queued": 0, "message": "Nothing selected."}

    for dn_name in dn_names:
        frappe.has_permission("Delivery Note", "submit", doc=dn_name, throw=True)

    frappe.enqueue(
        "shopify_integration.utils.fulfillment._fulfil_batch",
        queue="long",
        timeout=max(600, len(dn_names) * 20),
        dn_names=dn_names,
        triggered_by="bulk",
    )
    return {
        "queued": len(dn_names),
        "message": f"Queued {len(dn_names)} Delivery Note(s) for Shopify fulfillment.",
    }


def _fulfil_batch(dn_names: list, triggered_by: str = "bulk"):
    """Background worker for the bulk action."""
    for dn_name in (dn_names or []):
        try:
            fulfil_delivery_note(dn_name, triggered_by=triggered_by)
        except Exception:
            # fulfil_delivery_note already contains its own failures; this is
            # only a backstop so one bad document cannot end the batch.
            frappe.log_error(
                frappe.get_traceback(), f"Shopify: Bulk Fulfillment Failed — {dn_name}"
            )


@frappe.whitelist()
def is_fulfillment_enabled() -> bool:
    """Does any active store have fulfillment on?  Used by the list view."""
    return bool(
        frappe.db.exists("Shopify Settings", {"enable_sync": 1, "enable_fulfillment": 1})
    )
