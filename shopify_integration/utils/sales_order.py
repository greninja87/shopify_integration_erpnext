"""
sales_order.py — Sales Order creation from Shopify order data.

Flow:
 1.  Duplicate check  (shopify_order_id already exists and is not cancelled → skip)
 2.  Get or create customer  (phone-first, name from billing_address)
 3.  Map line items  — SKU must match item_code, item must have Item Tax Template
 4.  Back-calculate base rate from Shopify's tax-inclusive price:
        base_rate = (price × qty − discount) / (1 + tax_rate / 100) / qty
     e.g. ₹1180 at 18 % GST → base_rate = ₹1000, ERPNext adds ₹180 tax → ₹1180
 5.  Append the shipping row with its own tax-exclusive rate & item_tax_template
 6.  Reconcile all rows (items + shipping) to Shopify's `total_price` to the paisa
 7.  Taxes themselves are resolved by ERPNext / India Compliance via
     set_missing_values() — intra/inter-state template is chosen based on
     customer GST state vs company GSTIN.
 8.  Resolve payment terms from Shopify financial_status
 9.  Set billing / shipping / dispatch addresses
10.  Restore rates & kill phantom price-list/discount/margin fields
11.  Apply cost center, branch, naming series, custom field mappings
12.  Insert + Submit (or keep Draft)
13.  Send failure email if anything goes wrong
"""

import time

import frappe
from frappe.utils import nowdate, flt
from shopify_integration.utils.customer import (
    get_or_create_customer,
    find_or_create_address_for_order,
    addresses_are_different,
)
from shopify_integration.utils.item import (
    map_line_items,
    adjust_rows_to_match_total,
    get_item_and_tax,
)


# Beyond this many rupees, a Shopify-total vs Sales-Order-total difference is a
# data problem (Shopify charged tax on top of the price), not rounding drift.
# Crossing it triggers an Error Log entry AND a failure email.
TOTAL_MISMATCH_TOLERANCE = 0.05


# Maps Shopify financial_status → settings field name for payment terms
PAYMENT_TERMS_MAP = {
    "paid":           "payment_terms_paid",
    "partially_paid": "payment_terms_partial",
    "pending":        "payment_terms_pending",
    "voided":         "payment_terms_pending",
    "refunded":       "payment_terms_pending",
    "unpaid":         "payment_terms_pending",
}


def create_sales_order_from_shopify(order: dict, settings):
    """
    Create an ERPNext Sales Order from a Shopify orders/create webhook payload.

    :param order:    Full Shopify order dict
    :param settings: ShopifySettings document for this store
    """
    shopify_order_id   = str(order.get("id", ""))
    shopify_order_name = order.get("name", "")        # e.g. "#4609"
    financial_status   = order.get("financial_status", "pending")

    # ── 1. Duplicate check (exclude cancelled — allow cancel+amend) ───────────
    existing = frappe.db.get_value(
        "Sales Order",
        {"shopify_order_id": shopify_order_id, "docstatus": ["!=", 2]},
        "name"
    )
    if existing:
        # Duplicates are expected (Shopify re-sends webhooks); silently skip.
        # No Error Log / Shopify Log entry — this is not an error.
        return existing

    # ── 2. Customer ────────────────────────────────────────────────────────────
    # GST extraction happens BEFORE customer creation so the customer is created
    # with the correct legal name (Company type) + Shopify shipping address +
    # Shopify contact info all in one pass.
    billing_addr  = order.get("billing_address") or {}
    shipping_addr = order.get("shipping_address") or {}

    gstin               = None
    gst_legal_name      = None
    gst_customer_type   = "Individual"   # default; overridden by IC portal below
    if settings.get("gst_field_path"):
        try:
            from shopify_integration.utils.gst import extract_gstin, get_gst_customer_info
            gstin = extract_gstin(order, settings)
            if gstin:
                info           = get_gst_customer_info(gstin)
                gst_legal_name = info["legal_name"]
                gst_customer_type = info["customer_type"]
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Shopify: GST Name Lookup Failed — {shopify_order_name}")

    customer_name = get_or_create_customer(
        shopify_customer=order.get("customer"),
        billing_address=billing_addr,
        shipping_address=shipping_addr,
        settings=settings,
        gstin=gstin,
        gst_legal_name=gst_legal_name,
        gst_customer_type=gst_customer_type,
    )

    # ── 2b. GST billing address ────────────────────────────────────────────────
    # The customer is already created with the correct name.  Now find or create
    # the GST-registered billing address and link it to this customer.
    # Shipping address is never touched — it always comes from Shopify.
    gst_billing_addr = ""
    if gstin:
        try:
            from shopify_integration.utils.gst import resolve_billing_from_gstin
            gst_billing_addr = resolve_billing_from_gstin(gstin, customer_name) or ""
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Shopify: GST Address Failed — {shopify_order_name}")

    # ── 3. Totals we must hit ──────────────────────────────────────────────────
    # Shopify `total_price` is the GST-inclusive final amount the customer paid
    # (items + shipping + taxes − discounts).  ERPNext grand_total must equal this.
    order_discount   = flt(order.get("total_discounts") or 0)
    shipping_total   = _get_shipping_charges(order)     # GST-inclusive shipping
    shopify_total    = flt(order.get("total_price") or 0)

    # ── 4. Map items (tax-exclusive rates, per-line) ──────────────────────────
    items = map_line_items(
        order.get("line_items", []),
        settings,
        order_discount=order_discount,
        shopify_items_total=shopify_total - shipping_total,  # kept for compatibility
    )
    if not items:
        frappe.throw(
            f"No valid items for Shopify order {shopify_order_name}. "
            "All line items were skipped due to missing SKUs or Item not found.",
            frappe.ValidationError
        )

    # ── 5. Shipping row (GST-inclusive → strip tax the same way) ──────────────
    shipping_row = _build_shipping_row(shipping_total, settings)
    if shipping_row:
        items.append(shipping_row)

    # ── 6. Reconcile totals so ERPNext grand_total == Shopify total_price ─────
    # A material residual here means Shopify's total_price cannot be explained by
    # the line items at all (typically Shopify charged tax on top of a price this
    # app treats as tax-inclusive).  The SO is still created — but the residual is
    # re-checked after insert and reported by email in step 18b.
    pre_insert_residual = adjust_rows_to_match_total(items, shopify_total)

    # ── 7. Payment terms ───────────────────────────────────────────────────────
    terms_field   = PAYMENT_TERMS_MAP.get(financial_status, "payment_terms_pending")
    payment_terms = settings.get(terms_field) or ""

    # ── 8. Build SO items (strip internal _ keys; keep tax template) ──────────
    so_items = []
    for item in items:
        # Default price_list_rate to rate for shipping rows (which don't carry price_list_rate)
        price_list_rate = item.get("price_list_rate", item["rate"])

        so_items.append({
            "item_code":         item["item_code"],
            "item_name":         item["item_name"],
            "qty":               item["qty"],
            "rate":              item["rate"],       # tax-exclusive, already reconciled
            "uom":               item["uom"],
            "item_tax_template": item.get("_tax_template") or "",
            # Shopify line_item.id, set by map_line_items().  Blank on the
            # shipping row, which has no Shopify line item.  Populated even while
            # fulfillment is disabled, so the feature works accurately from day
            # one if it is ever switched on — orders synced without it can only
            # fall back to SKU matching.
            "custom_shopify_line_item_id": item.get("custom_shopify_line_item_id") or "",
            "price_list_rate":    price_list_rate,
            "discount_percentage": 0,
            "discount_amount":     0,
            "margin_type":         "",
            "margin_rate_or_amount": 0,
            "rate_with_margin":    0,
        })

    # ── 9. Transaction date ────────────────────────────────────────────────────
    created_at       = order.get("created_at", "")
    transaction_date = created_at[:10] if created_at else nowdate()

    # ── 10. Addresses ──────────────────────────────────────────────────────────
    # Both billing and shipping are resolved from THIS order's Shopify data so
    # repeat customers with a different address always get the correct address on
    # the SO, not the one from their first-ever order.
    #
    # Billing:
    #   B2B  → gst_billing_addr (GSTIN-registered address, resolved earlier)
    #   B2C  → find or create from this order's billing_addr
    if not gst_billing_addr and billing_addr:
        try:
            gst_billing_addr = find_or_create_address_for_order(
                customer_name=customer_name,
                shopify_address=billing_addr,
                address_type="Billing",
                is_primary=False,
                is_shipping=False,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Shopify: Billing Address Resolution Failed — {shopify_order_name}",
            )

    customer_billing_address = gst_billing_addr or ""

    # If address resolution failed entirely (create failed AND no existing linked
    # address was found), surface a clear error now rather than letting the SO
    # fail later with a cryptic "shipping_address_name" MandatoryError.
    # This also gives the operator a meaningful message to act on: fix the address
    # in ERPNext (link a correct Address to the customer) and retry the Shopify Log.
    if not customer_billing_address and billing_addr:
        frappe.throw(
            f"Could not resolve a billing address for Shopify order {shopify_order_name} "
            f"(customer: {customer_name}). "
            "Check Error Log → 'Shopify: Address Creation Failed' for the root cause. "
            "Fix: open the Customer in ERPNext, manually add the correct billing address, "
            "then retry this Shopify Log.",
            frappe.ValidationError,
        )

    # Shipping:
    #   When shipping != billing → find or create from this order's shipping_addr
    #   When shipping == billing → reuse the billing address (mark it as shipping)
    order_shipping_addr = ""
    if shipping_addr:
        # B2B orders (gst_billing_addr set): the ERPNext billing address is the
        # GST-registered address from the IC portal — NOT the Shopify billing_addr.
        # Comparing Shopify billing vs Shopify shipping is therefore meaningless;
        # always resolve shipping directly from Shopify's shipping_addr so the
        # GST portal address is never silently reused as the shipping address.
        if gst_billing_addr or addresses_are_different(billing_addr, shipping_addr):
            try:
                order_shipping_addr = find_or_create_address_for_order(
                    customer_name=customer_name,
                    shopify_address=shipping_addr,
                    address_type="Shipping",
                    is_primary=False,
                    is_shipping=True,
                )
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Shopify: Shipping Address Resolution Failed — {shopify_order_name}",
                )
        else:
            order_shipping_addr = customer_billing_address
            if customer_billing_address:
                frappe.db.set_value("Address", customer_billing_address, "is_shipping_address", 1)

    shipping_address_name = order_shipping_addr or customer_billing_address or ""

    # ── 11. Build the SO document ──────────────────────────────────────────────
    so_doc = {
        "doctype":           "Sales Order",
        "naming_series":     settings.naming_series or "SAL-ORD-.YYYY.-",
        "customer":          customer_name,
        "company":           settings.company,
        "set_warehouse":     settings.get("warehouse") or "",
        "transaction_date":  transaction_date,
        "delivery_date":     transaction_date,
        "currency":          order.get("currency", "INR"),
        "items":             so_items,

        # PO No carries the human-readable Shopify order name (#4609)
        "po_no": shopify_order_name,

        # Payment terms
        "payment_terms_template": payment_terms,

        # Addresses
        "company_address":       settings.get("company_address") or "",
        "customer_address":      customer_billing_address or "",
        "shipping_address_name": shipping_address_name,
        "dispatch_address_name": (
            settings.get("dispatch_address") or
            settings.get("company_address") or ""
        ),

        # Price list — we still set it for reporting, but every row's
        # price_list_rate will be forced equal to `rate` so ERPNext can't
        # derive a phantom discount percentage against it.
        "selling_price_list":  settings.selling_price_list or "Standard Selling",
        "ignore_pricing_rule": 1,

        # Shopify reference fields
        "shopify_order_id": shopify_order_id,
        "shopify_store":    settings.shop_domain or "",
    }

    # ── 12. Accounting dimensions ──────────────────────────────────────────────
    # Apply every accounting dimension that is configured in Shopify Settings.
    # We read directly from the Accounting Dimension doctype so new dimensions
    # added to ERPNext are picked up automatically without code changes.
    # cost_center is a core ERPNext field (not an Accounting Dimension row) —
    # handled separately below.
    if settings.get("cost_center"):
        so_doc["cost_center"] = settings.cost_center

    if frappe.db.exists("DocType", "Accounting Dimension"):
        all_dims = frappe.get_all(
            "Accounting Dimension",
            filters={"disabled": 0},
            fields=["fieldname"],
        )
        for dim in all_dims:
            fn = dim.get("fieldname") or ""
            if fn and settings.get(fn):
                so_doc[fn] = settings.get(fn)

    so = frappe.get_doc(so_doc)

    # ── Snapshot our Shopify-derived values BEFORE set_missing_values ─────────
    # set_missing_values() triggers ERPNext's price-list and pricing-rule
    # lookups, which would overwrite rate / price_list_rate / discount fields.
    # We snapshot, let ERPNext run its defaults (for tax / address / etc.),
    # and then restore.
    _snapshot = [
        {
            "rate":            flt(row.rate),
            "price_list_rate": flt(row.price_list_rate),
            "tax_template":    row.get("item_tax_template") or "",
        }
        for row in so.items
    ]

    # Let ERPNext / India Compliance run their defaults and tax resolution.
    #
    # ── Permission workaround for webhook / non-admin context ─────────────
    # set_missing_values() calls _get_party_details() which calls
    # frappe.has_permission("Customer", ...) via frappe.__init__.has_permission.
    # That function resolves permissions from frappe.session.user — it does NOT
    # honour frappe.flags.ignore_permissions (that flag is only checked in the
    # frappe.permissions module, which is a different call path).
    #
    # frappe.set_user() would fix the user but writes to Redis immediately via
    # session_obj.update_session(), corrupting other logged-in users' sessions.
    #
    # Safe fix: swap frappe.session.user directly.  frappe.session is a plain
    # Python object living in frappe.local (thread-local, request-scoped).
    # Assigning .user on it is just a Python attribute write — zero Redis
    # involvement, zero session side effects.  We restore the original value
    # in a finally block so the change is invisible outside this call.
    _prev_session_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        so.set_missing_values()
    finally:
        frappe.session.user = _prev_session_user

    # ── 13. Restore rates & apply Shopify discount natively ───────
    for row, snap in zip(so.items, _snapshot):
        shopify_rate    = snap["rate"]
        price_list_rate = snap["price_list_rate"]
        
        # If the rounder nudged rate above base, correct base
        if shopify_rate > price_list_rate:
            price_list_rate = shopify_rate

        row.rate                 = shopify_rate
        row.price_list_rate      = price_list_rate
        row.base_price_list_rate = price_list_rate
        row.base_rate            = shopify_rate
        
        discount_amount = price_list_rate - shopify_rate
        row.discount_amount = discount_amount
        if price_list_rate > 0 and discount_amount > 0:
            row.discount_percentage = round((discount_amount / price_list_rate) * 100.0, 2)
        else:
            row.discount_percentage = 0.0

        row.margin_type          = ""
        row.margin_rate_or_amount = 0
        row.rate_with_margin     = 0
        row.base_rate_with_margin = 0
        row.pricing_rules        = ""
        # Re-assert the item_tax_template we chose (ERPNext can drop it
        # during set_missing_values on items that have a company-specific
        # taxes table mismatch).
        if snap["tax_template"]:
            row.item_tax_template = snap["tax_template"]

    # ── 14. Accounting dimensions on each item row ─────────────────────────────
    # Mirror whatever was applied to the SO header onto every item row.
    # ERPNext expects the dimension on both header and child rows.
    if frappe.db.exists("DocType", "Accounting Dimension"):
        all_dims = frappe.get_all(
            "Accounting Dimension",
            filters={"disabled": 0},
            fields=["fieldname"],
        )
        dim_values = {
            dim["fieldname"]: settings.get(dim["fieldname"])
            for dim in all_dims
            if dim.get("fieldname") and settings.get(dim["fieldname"])
        }
        if dim_values:
            for item_row in so.items:
                for fn, val in dim_values.items():
                    item_row.set(fn, val)

    # ── 15. Custom field mapping ───────────────────────────────────────────────
    _apply_field_mapping(so, order, settings)

    so.flags.ignore_permissions = True

    # Insert — if the configured payment terms template generates duplicate due
    # dates, clear it and retry so the SO is not blocked.
    #
    # ── Permission workaround: account_perm_check on insert (ERPNext v15) ─────
    # so.insert() runs validate() → set_payment_schedule() → get_party_account_currency()
    # → get_party_account() → account_perm_check(account), which calls
    # frappe.has_permission("Account", ..., account) against frappe.session.user.
    # This does NOT honour so.flags.ignore_permissions (same limitation as the
    # set_missing_values() call above — see the comment there). ERPNext added
    # this check in accounts/party.py (upstream PR frappe/erpnext#56745,
    # shipped in a v15 patch release) as a security hardening fix, and it now
    # fires on every insert, not just set_missing_values(). Whether triggered
    # by a live webhook (Guest session) or a manual Retry Order click (a real
    # desk user who may not have Accounts-module read access), the session
    # user must be swapped to Administrator for the duration of the insert.
    _prev_session_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        try:
            so.insert()
        except frappe.ValidationError as e:
            if "duplicate due dates" in str(e).lower():
                frappe.log_error(
                    f"Payment terms template '{so.payment_terms_template}' caused duplicate "
                    f"due dates for Shopify order {shopify_order_name}. "
                    "Retrying without payment terms.",
                    "Shopify: Payment Terms Warning"
                )
                so.payment_terms_template = ""
                so.payment_schedule = []
                so.insert()
            else:
                raise
    finally:
        frappe.session.user = _prev_session_user

    # Commit the draft SO before submitting.  Without this, a QueryDeadlockError
    # during submit causes frappe.db.rollback() to undo the insert as well —
    # so.reload() then raises DoesNotExistError and the retry loop never recovers.
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — isolates insert from submit transaction so deadlock retries are safe

    # ── 16. Final total reconciliation guard ──────────────────────────────────
    # If somehow ERPNext's tax pass still leaves a paisa off (for example when
    # India Compliance splits 18% into 9%+9% and each rounds independently),
    # nudge the last row one more time and re-save.  This keeps SO grand_total
    # exactly equal to Shopify total_price.
    total_mismatch = 0.0
    if shopify_total > 0:
        diff = round(flt(shopify_total) - flt(so.grand_total), 2)
        if diff != 0:
            _absorb_paisa_on_submitted_doc(so, shopify_total)
            # save() re-runs validate() → account_perm_check — same swap as insert() above.
            _prev_session_user = frappe.session.user
            try:
                if frappe.session.user in ("Guest", None, ""):
                    frappe.session.user = "Administrator"
                so.save(ignore_permissions=True)
            finally:
                frappe.session.user = _prev_session_user

        # Re-check AFTER the absorber ran.  This diff used to be computed, acted
        # on, and never verified — so an unreconcilable order produced a Sales
        # Order for the wrong amount with no error raised anywhere.
        total_mismatch = round(flt(shopify_total) - _so_payable_total(so), 2)

    # ── 17. Submit or keep Draft ──────────────────────────────────────────────
    should_keep_draft = (
        (financial_status == "paid"           and settings.get("keep_draft_paid")) or
        (financial_status == "partially_paid" and settings.get("keep_draft_partial")) or
        (financial_status in ("pending", "voided", "unpaid") and settings.get("keep_draft_pending"))
    )

    if not should_keep_draft:
        # Retry submit for transient tabBin deadlocks.  When multiple Shopify
        # orders processing concurrently touch the same item-warehouse bin,
        # MySQL optimistic locking raises error 1020.  The contention clears
        # within milliseconds — three attempts with a short backoff is enough.
        # submit() re-runs validate() → account_perm_check — same swap as insert() above.
        _prev_session_user = frappe.session.user
        try:
            if frappe.session.user in ("Guest", None, ""):
                frappe.session.user = "Administrator"
            _MAX_SUBMIT_RETRIES = 3
            for _attempt in range(_MAX_SUBMIT_RETRIES):
                try:
                    so.submit()
                    break
                except frappe.QueryDeadlockError:
                    if _attempt >= _MAX_SUBMIT_RETRIES - 1:
                        raise
                    frappe.db.rollback()
                    time.sleep(0.4 * (_attempt + 1))  # 0.4 s, 0.8 s
                    so.reload()
        finally:
            frappe.session.user = _prev_session_user

    # ── 18. Payment Entry (optional — controlled by Shopify Settings) ─────────
    # Creates a Payment Entry against the submitted SO for paid/partially_paid
    # Shopify orders.  pe_name is "" when PE was skipped or failed.
    pe_name = ""
    if (not should_keep_draft
            and settings.get("enable_payment_entry")
            and financial_status in ("paid", "partially_paid")):
        try:
            from shopify_integration.utils.payment_entry import (
                create_payment_entry_from_shopify,
            )
            pe_name = create_payment_entry_from_shopify(so, order, settings)
        except Exception:
            # PE failure must never block the SO — log and notify.
            tb = frappe.get_traceback()
            frappe.log_error(tb, f"Shopify: Payment Entry Failed — {so.name}")
            _send_payment_entry_failure_email(settings, order, so.name, tb)

    # ── 18b. Total / payment mismatch alarm ──────────────────────────
    # Runs on every order, including drafts and PE-disabled setups.  This is the
    # single place that guarantees "money collected != Sales Order value" is never
    # silent.  Never allowed to break the flow.
    try:
        _check_total_and_payment_mismatch(
            so, order, settings,
            pe_name=pe_name,
            shopify_total=shopify_total,
            total_mismatch=total_mismatch,
            pre_insert_residual=pre_insert_residual,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Mismatch Check Failed — {so.name}",
        )

    # ── 19. Sales Invoice — "After Payment Entry" flow (Option B) ─────────────
    # Trigger conditions (both must be true):
    #   a) SI is enabled and trigger is "After Payment Entry"
    #   b) Either PE was just created (pe_name set), OR PE is not enabled
    #      (SI runs independently — no PE required)
    _si_enabled   = settings.get("enable_sales_invoice") and not should_keep_draft
    _si_trigger   = settings.get("sales_invoice_trigger") == "After Payment Entry"
    _pe_satisfied = bool(pe_name) or not settings.get("enable_payment_entry")
    if (_si_enabled and _si_trigger and _pe_satisfied):
        try:
            from shopify_integration.utils.sales_invoice import (
                create_sales_invoice_from_so,
                _send_si_failure_email,
            )
            create_sales_invoice_from_so(so, settings, pe_name=pe_name)
        except Exception:
            tb = frappe.get_traceback()
            frappe.log_error(tb, f"Shopify: Sales Invoice Failed — {so.name}")
            _send_si_failure_email(settings, "Sales Order", so.name, tb)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; SO must persist before PE/SI creation in same job
    # Successful SO creation does NOT write to Error Log or Shopify Log.
    # Only real failures are logged so the admin's logs stay meaningful.
    return so.name


# ── Shipping row ───────────────────────────────────────────────────────────────

def _build_shipping_row(shipping_total: float, settings) -> dict:
    """
    Build a shipping row whose `rate` is tax-exclusive and whose
    item_tax_template is resolved from the configured shipping Item.
    Skips if shipping is zero or no shipping Item is configured.
    """
    if not shipping_total or not settings.get("shipping_item"):
        return None

    ship_item = get_item_and_tax(settings.shipping_item, settings.company)
    if not ship_item:
        # No matching ERPNext item for shipping; fall back to a flat row
        # with no tax template so ERPNext treats it as untaxed.
        return {
            "item_code":    settings.shipping_item,
            "item_name":    "Shipping",
            "qty":          1,
            "rate":         round(flt(shipping_total), 2),
            "uom":          "Nos",
            "_tax_template": "",
            "_tax_rate":     0.0,
        }

    tax_rate = flt(ship_item["tax_rate"])
    # Shopify ships its shipping_lines[].price as tax-inclusive when the
    # store has `taxes_included`.  Strip GST the same way we strip it off
    # the line items.
    if tax_rate > 0:
        rate_excl = round(flt(shipping_total) / (1.0 + tax_rate / 100.0), 2)
    else:
        rate_excl = round(flt(shipping_total), 2)

    return {
        "item_code":    ship_item["item_code"],
        "item_name":    ship_item["item_name"] or "Shipping",
        "qty":          1,
        "rate":         rate_excl,
        "uom":          ship_item["uom"],
        "_tax_template": ship_item["tax_template"],
        "_tax_rate":     tax_rate,
    }


# ── Paisa absorber for post-insert drift ──────────────────────────────────────

def _absorb_paisa_on_submitted_doc(so, shopify_total: float):
    """
    Run the full multi-row absorber on an already-inserted SO so its
    grand_total converges back to `shopify_total` exactly.  Used as a
    belt-and-suspenders in case ERPNext's tax rounding diverges from ours.

    Does not run .save() itself — caller is responsible so flags/logic
    stay in one place.
    """
    # Project SO child rows into the dict shape adjust_rows_to_match_total wants.
    # Use the same effective-rate derivation as item.py (_get_item_tax_template):
    # sum CGST+SGST rows as intra_total, IGST rows as inter_total, take the max.
    tax_rate_cache = {}

    def tax_rate_for(template_name: str) -> float:
        if not template_name:
            return 0.0
        if template_name in tax_rate_cache:
            return tax_rate_cache[template_name]
        rows = frappe.get_all(
            "Item Tax Template Detail",
            filters={"parent": template_name},
            fields=["tax_rate", "tax_type"],
            order_by="idx asc",
        )
        intra, inter, raw = 0.0, 0.0, 0.0
        for r in rows:
            rate = flt(r.get("tax_rate", 0))
            account = (r.get("tax_type") or "").upper()
            # Skip RCM and Input Tax rows — same logic as item.py
            if rate <= 0 or "RCM" in account or "INPUT" in account:
                continue
            raw += rate
            if "IGST" in account:
                inter += rate
            elif "CGST" in account or "SGST" in account or "UTGST" in account:
                intra += rate
        effective = max(intra, inter) or raw
        tax_rate_cache[template_name] = effective
        return effective

    # Detect whether India Compliance applied CGST+SGST (intra-state, split
    # rounding) or IGST (inter-state, single rounding).  This determines how
    # we simulate the per-row tax so the absorber's search targets the actual
    # ERPNext grand_total rather than a simulation that diverges by ₹0.01.
    #
    # For a split-tax SO (intra-state), ERPNext rounds each half separately:
    #   CGST = round(amount × half_rate, 2)
    #   SGST = round(amount × half_rate, 2)
    # This can differ by ₹0.01 from round(amount × full_rate, 2).
    # Without the split flag the absorber would keep a rate that its own
    # simulation thinks is perfect but that ERPNext still computes as off.
    _cgst_or_sgst_applied = any(
        flt(t.tax_amount) > 0
        and ("CGST" in (t.account_head or "").upper()
             or "SGST" in (t.account_head or "").upper())
        for t in so.taxes
    )

    proxy_rows = []
    for row in so.items:
        proxy_rows.append({
            "rate":       flt(row.rate),
            "qty":        flt(row.qty),
            "_tax_rate":  tax_rate_for(row.item_tax_template or ""),
            "_split_tax": _cgst_or_sgst_applied,
        })

    adjust_rows_to_match_total(proxy_rows, shopify_total)

    # Push the adjusted rates back onto the child rows, and keep all the
    # phantom-discount fields zeroed so ERPNext's re-validate() doesn't
    # regenerate them.
    for row, proxy in zip(so.items, proxy_rows):
        new_rate = flt(proxy["rate"])
        if new_rate == flt(row.rate):
            continue
            
        # Re-sync discount fields against the nudged rate
        price_list_rate = flt(row.price_list_rate)
        if new_rate > price_list_rate:
            price_list_rate = new_rate
            row.price_list_rate = price_list_rate
            row.base_price_list_rate = price_list_rate
            
        row.rate                 = new_rate
        row.base_rate            = new_rate
        
        discount_amount = price_list_rate - new_rate
        row.discount_amount = discount_amount
        if price_list_rate > 0 and discount_amount > 0:
            row.discount_percentage = round((discount_amount / price_list_rate) * 100.0, 2)
        else:
            row.discount_percentage = 0.0

        row.margin_type          = ""
        row.margin_rate_or_amount = 0
        row.rate_with_margin     = 0
        row.base_rate_with_margin = 0


# ── Misc helpers ───────────────────────────────────────────────────────────────

def _get_shipping_charges(order: dict) -> float:
    """Sum all shipping line prices from the Shopify order."""
    return sum(flt(line.get("price", 0)) for line in order.get("shipping_lines", []))


def _apply_field_mapping(so, order: dict, settings):
    """Apply custom Shopify → ERPNext field mappings configured in Shopify Settings."""
    try:
        for mapping in (settings.field_mapping or []):
            path          = mapping.get("shopify_field_path", "")
            erpnext_field = mapping.get("erpnext_field", "")
            target_dt     = mapping.get("target_doctype", "")
            if target_dt != "Sales Order" or not path or not erpnext_field:
                continue
            value = _get_nested_value(order, path)
            if value is not None:
                so.set(erpnext_field, value)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Field Mapping Error")


def _get_nested_value(data: dict, path: str):
    """
    Traverse a dot-separated path through the Shopify payload.

    Dict keys are looked up by name; a numeric segment indexes into a list, so
    values that live inside arrays are reachable from a Field Mapping row:

        discount_applications.0.title  -> manual / staff-applied discount code
        discount_codes.0.code          -> coupon-code discount
        shipping_lines.0.title         -> shipping method name
        note_attributes.0.value        -> cart attribute

    Negative indices work too (`.-1.` for the last element).

    Returns None when a key is missing, an index is out of range, or the path
    tries to walk past a scalar.  _apply_field_mapping treats None as "leave
    the target field alone", so an unmatched path is a no-op rather than an
    error.
    """
    val = data
    for key in path.split("."):
        if isinstance(val, dict):
            val = val.get(key)
        elif isinstance(val, list) and key.lstrip("-").isdigit():
            idx = int(key)
            if not (-len(val) <= idx < len(val)):
                return None
            val = val[idx]
        else:
            return None
    return val


# ── Failure email ──────────────────────────────────────────────────────────────

def send_failure_email(settings, order: dict, error_message: str):
    """Send an email when a Shopify order fails to create a Sales Order."""
    to_emails = (settings.get("failure_email_to") or "").strip()
    if not to_emails:
        return

    order_name    = order.get("name", "Unknown")
    order_id      = order.get("id", "")
    shop          = settings.get("shop_domain") or settings.get("store_name") or "Shopify"
    cc_emails     = (settings.get("failure_email_cc") or "").strip()
    cc_list       = [e.strip() for e in cc_emails.split(",") if e.strip()] if cc_emails else []
    subject       = f"[Shopify] Failed to create Sales Order for {order_name} — {shop}"

    customer_data = order.get("customer") or {}
    billing       = order.get("billing_address") or {}
    customer_name = (
        billing.get("name") or
        f"{customer_data.get('first_name','')} {customer_data.get('last_name','')}".strip() or
        "Unknown"
    )
    total      = order.get("total_price", "")
    items_list = "<br>".join(
        f"&bull; {li.get('name') or li.get('title')} | SKU: {li.get('sku') or 'N/A'} | ₹{li.get('price')}"
        for li in order.get("line_items", [])
    )

    message = f"""
    <p>A Shopify order could not be automatically created in ERPNext.</p>
    <table border="0" cellpadding="4" style="font-family:Arial;font-size:13px;">
      <tr><td><b>Shopify Order</b></td><td>{order_name}</td></tr>
      <tr><td><b>Order ID</b></td><td>{order_id}</td></tr>
      <tr><td><b>Store</b></td><td>{shop}</td></tr>
      <tr><td><b>Customer</b></td><td>{customer_name}</td></tr>
      <tr><td><b>Total</b></td><td>&#8377;{total}</td></tr>
      <tr><td><b>Items</b></td><td>{items_list}</td></tr>
    </table>
    <p><b>Failure Reason:</b></p>
    <pre style="background:#fef2f2;padding:10px;border-left:4px solid #ef4444;font-size:12px;">{error_message[:2000]}</pre>
    <p style="color:#6b7280;font-size:12px;">
      Go to ERPNext &rarr; Shopify Log to view the full payload and retry.
    </p>
    """
    try:
        frappe.sendmail(
            recipients=[e.strip() for e in to_emails.split(",") if e.strip()],
            cc=cc_list,
            subject=subject,
            message=message,
            delayed=False,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Failure Email Send Error")


# ── Total / payment mismatch detection ────────────────────────────

def _so_payable_total(so) -> float:
    """
    The figure ERPNext actually settles a Payment Entry against:
    rounded_total when rounding is enabled, else grand_total.
    Mirrors the same derivation used in payment_entry.py.
    """
    return flt(so.get("rounded_total")) or flt(so.get("grand_total"))


def _check_total_and_payment_mismatch(
    so, order: dict, settings, pe_name: str,
    shopify_total: float, total_mismatch: float,
    pre_insert_residual: float = 0.0,
):
    """
    Alarm when the money Shopify collected does not equal what the Sales Order
    (and therefore the Payment Entry) is worth.

    Two independent gaps are checked:

      1. total_gap   = Shopify total_price - Sales Order payable total
         Non-zero means the SO was written for the wrong amount.  The dominant
         cause is a Shopify product with "Charge tax on this product" enabled
         while store prices are GST-inclusive: Shopify adds GST on top, while
         this app backs GST out of that same price.

      2. payment_gap = Shopify amount paid - Payment Entry paid_amount
         Non-zero means part of the received money was never recorded.

    Either gap crossing TOTAL_MISMATCH_TOLERANCE writes an Error Log entry and
    sends a failure email.  This function never raises.
    """
    so_total  = _so_payable_total(so)
    total_gap = round(flt(total_mismatch), 2)

    # What Shopify says it collected, using the same derivation the Payment
    # Entry uses, so the two numbers are directly comparable.
    try:
        from shopify_integration.utils.payment_entry import _get_amount_paid
        shopify_paid = flt(_get_amount_paid(order))
    except Exception:
        shopify_paid = round(
            flt(order.get("total_price")) - flt(order.get("total_outstanding")), 2
        )

    pe_paid = 0.0
    if pe_name:
        pe_paid = flt(frappe.db.get_value("Payment Entry", pe_name, "paid_amount"))

    # Only meaningful when a PE was actually created.
    payment_gap = round(shopify_paid - pe_paid, 2) if pe_name else 0.0

    if (abs(total_gap) <= TOTAL_MISMATCH_TOLERANCE
            and abs(payment_gap) <= TOTAL_MISMATCH_TOLERANCE):
        return

    # ── Root-cause diagnostics ──────────────────────────────────
    # `taxes_included` is a top-level boolean on the Shopify order payload.
    # False + a non-zero total_tax is the signature of the tax-on-top case.
    taxes_included = order.get("taxes_included")
    shopify_tax    = flt(order.get("total_tax") or 0)
    tax_on_top     = (taxes_included is False and shopify_tax > 0)

    # Lead with the plain ERPNext-side statement: how the Payment Entry
    # compares to the Sales Order.  That is the discrepancy the accounts team
    # actually sees in ERPNext, so it belongs first.
    pe_vs_so = round(pe_paid - so_total, 2) if pe_name else 0.0
    if pe_name and pe_vs_so > TOTAL_MISMATCH_TOLERANCE:
        lead = (
            f"Payment Entry {pe_name} ({pe_paid:.2f}) is MORE than Sales Order "
            f"{so.name} ({so_total:.2f}) in ERPNext - difference {pe_vs_so:.2f}. "
        )
    elif pe_name and pe_vs_so < -TOTAL_MISMATCH_TOLERANCE:
        lead = (
            f"Payment Entry {pe_name} ({pe_paid:.2f}) is LESS than Sales Order "
            f"{so.name} ({so_total:.2f}) in ERPNext - difference "
            f"{abs(pe_vs_so):.2f}. "
        )
    else:
        lead = (
            f"Shopify total ({flt(shopify_total):.2f}) does not match Sales "
            f"Order {so.name} ({so_total:.2f}) in ERPNext. "
        )

    if tax_on_top:
        likely_cause = (
            lead +
            "Shopify charged tax on top of the product price (total_tax = "
            f"{shopify_tax:.2f}). Check whether this item was supposed to be "
            "taxable in Shopify."
        )
    elif abs(total_gap) > TOTAL_MISMATCH_TOLERANCE:
        likely_cause = (
            lead +
            "Shopify total_price does not equal the sum of "
            "(price x qty - discount) over the line items plus shipping. Check "
            "whether this item was supposed to be taxable in Shopify, and check "
            "the payload for order-level discounts, tips, duties, gift cards or "
            "refunds that this app does not currently map."
        )
    else:
        likely_cause = (
            lead +
            "Sales Order total matches Shopify, but the Payment Entry amount "
            "differs from what Shopify collected. Check the payment schedule / "
            "payment terms template on the Sales Order."
        )

    detail = (
        f"Shopify order      : {order.get('name')} (id {order.get('id')})\n"
        f"Sales Order        : {so.name}\n"
        f"Payment Entry      : {pe_name or '(none created)'}\n"
        f"\n"
        f"Shopify total_price      : {flt(shopify_total):.2f}\n"
        f"Sales Order payable total: {so_total:.2f}\n"
        f"  -> total_gap           : {total_gap:.2f}\n"
        f"\n"
        f"Shopify amount paid      : {shopify_paid:.2f}\n"
        f"Payment Entry paid_amount: {pe_paid:.2f}\n"
        f"  -> payment_gap         : {payment_gap:.2f}\n"
        f"  -> PE minus Sales Order: {pe_vs_so:.2f}\n"
        f"\n"
        f"Shopify taxes_included   : {taxes_included}\n"
        f"Shopify total_tax        : {shopify_tax:.2f}\n"
        f"Rounding residual (pre-insert): {flt(pre_insert_residual):.2f}\n"
        f"\n"
        f"Likely cause: {likely_cause}"
    )

    frappe.log_error(detail, "Shopify: Payment / Order Total Mismatch")
    _send_total_mismatch_email(
        settings, order, so, pe_name,
        shopify_total=flt(shopify_total),
        so_total=so_total,
        shopify_paid=shopify_paid,
        pe_paid=pe_paid,
        total_gap=total_gap,
        payment_gap=payment_gap,
        pe_vs_so=pe_vs_so,
        taxes_included=taxes_included,
        shopify_tax=shopify_tax,
        likely_cause=likely_cause,
    )


def _send_total_mismatch_email(
    settings, order: dict, so, pe_name: str,
    shopify_total: float, so_total: float,
    shopify_paid: float, pe_paid: float,
    total_gap: float, payment_gap: float, pe_vs_so: float,
    taxes_included, shopify_tax: float, likely_cause: str,
):
    """
    Email the admin when payment received and Sales Order total disagree.
    Reuses failure_email_to / failure_email_cc from Shopify Settings.
    """
    to_emails = (settings.get("failure_email_to") or "").strip()
    if not to_emails:
        return

    order_name = order.get("name", "Unknown")
    shop       = settings.get("shop_domain") or settings.get("store_name") or "Shopify"
    cc_emails  = (settings.get("failure_email_cc") or "").strip()
    cc_list    = [e.strip() for e in cc_emails.split(",") if e.strip()] if cc_emails else []
    worst      = total_gap if abs(total_gap) >= abs(payment_gap) else payment_gap
    subject    = (
        f"[Shopify] Payment / Order total MISMATCH for {order_name} "
        f"(off by \u20b9{abs(worst):.2f}) \u2014 {shop}"
    )

    def _row(label, value, highlight=False):
        style = "color:#b91c1c;font-weight:bold;" if highlight else ""
        return (f'<tr><td>{label}</td>'
                f'<td style="text-align:right;{style}">&#8377;{value:,.2f}</td></tr>')

    message = f"""
    <p><b>Payment received and the Sales Order total do not match.</b>
    Figures and likely cause below.</p>

    <table border="0" cellpadding="5" style="font-family:Arial;font-size:13px;border-collapse:collapse;">
      <tr><td><b>Shopify Order</b></td><td>{order_name}</td></tr>
      <tr><td><b>Store</b></td><td>{shop}</td></tr>
      <tr><td><b>Sales Order</b></td><td>{so.name}</td></tr>
      <tr><td><b>Payment Entry</b></td><td>{pe_name or '<i>none created</i>'}</td></tr>
      <tr><td><b>Customer</b></td><td>{so.customer}</td></tr>
    </table>

    <h4 style="margin-bottom:4px;">Order total</h4>
    <table border="0" cellpadding="5" style="font-family:Arial;font-size:13px;border-collapse:collapse;">
      {_row('Shopify total_price', shopify_total)}
      {_row('Sales Order total', so_total)}
      {_row('Difference', total_gap, highlight=abs(total_gap) > TOTAL_MISMATCH_TOLERANCE)}
    </table>

    <h4 style="margin-bottom:4px;">Payment</h4>
    <table border="0" cellpadding="5" style="font-family:Arial;font-size:13px;border-collapse:collapse;">
      {_row('Shopify amount paid', shopify_paid)}
      {_row('Payment Entry recorded', pe_paid)}
      {_row('Not recorded (Shopify minus PE)', payment_gap, highlight=abs(payment_gap) > TOTAL_MISMATCH_TOLERANCE)}
      {_row('Sales Order total', so_total)}
      {_row('Payment Entry minus Sales Order', pe_vs_so, highlight=abs(pe_vs_so) > TOTAL_MISMATCH_TOLERANCE)}
    </table>

    <h4 style="margin-bottom:4px;">Shopify tax flags</h4>
    <table border="0" cellpadding="5" style="font-family:Arial;font-size:13px;border-collapse:collapse;">
      <tr><td>taxes_included</td><td style="text-align:right;">{taxes_included}</td></tr>
      <tr><td>total_tax</td><td style="text-align:right;">&#8377;{shopify_tax:,.2f}</td></tr>
    </table>

    <p><b>Likely cause</b></p>
    <pre style="background:#fef2f2;padding:10px;border-left:4px solid #ef4444;font-size:12px;white-space:pre-wrap;">{likely_cause}</pre>

    <p style="color:#6b7280;font-size:12px;">
      Check whether this item was supposed to be taxable in Shopify, and fix
      the product's tax setting there if it was not. The full payload is on
      the Shopify Log.
    </p>
    """
    try:
        frappe.sendmail(
            recipients=[e.strip() for e in to_emails.split(",") if e.strip()],
            cc=cc_list,
            subject=subject,
            message=message,
            delayed=False,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Mismatch Email Send Error")


# ── Payment Entry failure email ───────────────────────────────────────────────

def _send_payment_entry_failure_email(settings, order: dict, so_name: str, error_message: str):
    """
    Send an email when Payment Entry creation fails after the Sales Order was created.
    Uses the same failure_email_to / failure_email_cc settings as the SO failure email.
    """
    to_emails = (settings.get("failure_email_to") or "").strip()
    if not to_emails:
        return

    order_name = order.get("name", "Unknown")
    order_id   = order.get("id", "")
    shop       = settings.get("shop_domain") or settings.get("store_name") or "Shopify"
    cc_emails  = (settings.get("failure_email_cc") or "").strip()
    cc_list    = [e.strip() for e in cc_emails.split(",") if e.strip()] if cc_emails else []
    subject    = f"[Shopify] Payment Entry Failed for {order_name} — {shop}"

    message = f"""
    <p>A Shopify Sales Order was created in ERPNext, but the
    <b>Payment Entry could not be created automatically</b>.</p>
    <p>Please create the Payment Entry manually in ERPNext.</p>
    <table border="0" cellpadding="4" style="font-family:Arial;font-size:13px;">
      <tr><td><b>Shopify Order</b></td><td>{order_name}</td></tr>
      <tr><td><b>Order ID</b></td><td>{order_id}</td></tr>
      <tr><td><b>Store</b></td><td>{shop}</td></tr>
      <tr><td><b>Sales Order</b></td><td>{so_name}</td></tr>
    </table>
    <p><b>Failure Reason:</b></p>
    <pre style="background:#fef2f2;padding:10px;border-left:4px solid #ef4444;font-size:12px;">{error_message[:2000]}</pre>
    <p style="color:#6b7280;font-size:12px;">
      Go to ERPNext &rarr; Shopify Log to view the full payload and retry.
    </p>
    """
    try:
        frappe.sendmail(
            recipients=[e.strip() for e in to_emails.split(",") if e.strip()],
            cc=cc_list,
            subject=subject,
            message=message,
            delayed=False,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Payment Entry Failure Email Send Error")


# ── DocType event hooks ────────────────────────────────────────────────────────

def clear_shopify_log_on_trash(doc, method=None):
    """
    Hook: Sales Order → on_trash.

    ERPNext's link-validator blocks deletion of a Sales Order when a Shopify Log
    row still holds a reference to it.  This hook clears that reference and
    flips the log's status back to 'Skipped' so it becomes retry-eligible
    (the Retry Order button is hidden while status == Processed).
    The Shopify Log itself is preserved for audit.
    """
    if not frappe.db.exists("DocType", "Shopify Log"):
        return
    log_names = frappe.get_all(
        "Shopify Log",
        filters={"erpnext_sales_order": doc.name},
        pluck="name",
    )
    for log_name in log_names:
        frappe.db.set_value("Shopify Log", log_name, {
            "erpnext_sales_order": "",
            "status":              "Skipped",
            "error_message":       f"Linked Sales Order {doc.name} was deleted — ready for retry.",
        }, update_modified=False)


def clear_shopify_fields_on_amend(doc, method=None):
    """
    Hook: Sales Order → before_insert.

    Clears shopify_order_id / shopify_store only on manual duplicates.

    Amended copies intentionally keep the Shopify fields — the original SO is
    cancelled (docstatus=2) when amended, so the duplicate check (docstatus != 2)
    correctly treats the amended copy as the active order for that Shopify order.

    Manual duplicates (Duplicate button) must be cleared: at before_insert time
    the new doc is not yet in the DB, so if shopify_order_id already exists on
    any non-cancelled SO it can only mean this is a copy, not a new webhook order.
    """
    if doc.get("amended_from"):
        return

    if doc.get("shopify_order_id"):
        already_exists = frappe.db.get_value(
            "Sales Order",
            {"shopify_order_id": doc.shopify_order_id, "docstatus": ["!=", 2]},
            "name",
        )
        if already_exists:
            doc.shopify_order_id = ""
            doc.shopify_store    = ""
