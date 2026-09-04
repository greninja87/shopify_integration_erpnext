import frappe


def before_uninstall():
    """
    Remove custom fields added by this app when it is uninstalled.
    Prevents orphaned fields from cluttering the Customer and Sales Order forms.
    """
    _SHOPIFY_CUSTOM_FIELDS = [
        # Customer
        "Customer-shopify_section",
        "Customer-shopify_customer_id",
        "Customer-shopify_phone",
        "Customer-shopify_email",
        # Sales Order
        "Sales Order-shopify_section",
        "Sales Order-shopify_order_id",
        "Sales Order-shopify_store",
        # Delivery Note
        "Delivery Note-shopify_section",
        "Delivery Note-shopify_order_id",
        "Delivery Note-shopify_store",
        # Payment Entry
        "Payment Entry-custom_shopify_gateway_section",
        "Payment Entry-custom_gateway_reference",
        "Payment Entry-custom_gateway_column_break",
        "Payment Entry-custom_gateway_name",
        # Sales Order Item
        "Sales Order Item-custom_shopify_line_item_id",
        # Delivery Note — Shopify fulfillment state
        "Delivery Note-custom_shopify_fulfillment_section",
        "Delivery Note-custom_shopify_fulfillment_status",
        "Delivery Note-custom_shopify_fulfillment_id",
        "Delivery Note-custom_shopify_fulfillment_column_break",
        "Delivery Note-custom_shopify_fulfilled_at",
        "Delivery Note-custom_shopify_fulfillment_error",
        # Refund Request (payment_portals) — Shopify refund write-back state
        "Refund Request-shopify_refund_writeback_section",
        "Refund Request-shopify_writeback_status",
        "Refund Request-shopify_refund_gid",
        "Refund Request-shopify_refund_writeback_column_break",
        "Refund Request-shopify_refund_gateway",
        "Refund Request-shopify_writeback_at",
        "Refund Request-shopify_writeback_error",
    ]
    for cf_name in _SHOPIFY_CUSTOM_FIELDS:
        if frappe.db.exists("Custom Field", cf_name):
            try:
                frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Shopify Integration: Could not remove custom field {cf_name} on uninstall"
                )
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — uninstall hook runs outside request lifecycle
    print("🗑️  Shopify Integration: Custom fields removed.")


def after_install():
    """
    Create / update all custom fields required for Shopify Integration.
    Safe to re-run on reinstall — create_or_update corrects existing fields
    (unique removed, insert_after moved, collapsible added) without losing data.
    Compatible with ERPNext v15 and v16.
    """
    _cleanup_deprecated_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — install hook; deprecated fields must be removed before creating new ones

    create_customer_custom_fields()
    create_sales_order_custom_fields()
    create_delivery_note_custom_fields()
    create_payment_entry_custom_fields()
    create_sales_order_item_custom_fields()
    create_delivery_note_fulfillment_custom_fields()
    create_refund_request_writeback_custom_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — install hook runs outside request lifecycle
    print("✅ Shopify Integration: Custom fields created / updated successfully.")


# ── Cleanup ────────────────────────────────────────────────────────────────────

def _cleanup_deprecated_fields():
    """Remove fields that are no longer used by this app."""
    deprecated = [
        # Item fields — SKU matched via item_code, tax via Item Tax Template rows
        "Item-shopify_sku",
        "Item-shopify_tax_template",
        "Item-shopify_section",
        # Sales Order — shopify_order_name is redundant; value already in po_no
        "Sales Order-shopify_order_name",
    ]
    for cf_name in deprecated:
        if frappe.db.exists("Custom Field", cf_name):
            frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True)
            print(f"  Removed deprecated custom field: {cf_name}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def create_or_update_custom_field(doctype, field_def):
    """
    Create a custom field if it doesn't exist; update if it does.
    Ensures reinstalls correct field properties without removing existing data.
    """
    fieldname = field_def.get("fieldname")
    cf_name   = f"{doctype}-{fieldname}"

    if frappe.db.exists("Custom Field", cf_name):
        cf      = frappe.get_doc("Custom Field", cf_name)
        changed = False
        for key, value in field_def.items():
            if str(cf.get(key) or "") != str(value or ""):
                cf.set(key, value)
                changed = True
        if changed:
            cf.save(ignore_permissions=True)
    else:
        cf = frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **field_def})
        cf.insert(ignore_permissions=True)


def _so_shopify_anchor() -> str:
    """
    Find the best insert_after anchor for the Shopify section in Sales Order.
    Tries several stable field names in order of preference so the section
    lands in More Info → Additional Info regardless of ERPNext version.
    """
    so_meta = frappe.get_meta("Sales Order")
    for fieldname in [
        "campaign",                      # ERPNext v15 More Info → Additional Info
        "inter_company_order_reference", # v14/v15 Additional Info
        "source",                        # very stable fallback
        "tc_name",                       # Terms section fallback
        "amendment_date",                # absolute last resort
    ]:
        if so_meta.get_field(fieldname):
            return fieldname
    return "amendment_date"


# ── Customer custom fields ─────────────────────────────────────────────────────

def create_customer_custom_fields():
    """Add collapsible Shopify section to Customer DocType."""
    doctype = "Customer"

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_section",
        "label":        "Shopify",
        "fieldtype":    "Section Break",
        "insert_after": "customer_details",
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_customer_id",
        "label":        "Shopify Customer ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_section",
        "read_only":    1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_phone",
        "label":        "Shopify Phone",
        "fieldtype":    "Data",
        "insert_after": "shopify_customer_id",
        "read_only":    1,
        "description":  "Phone number used as the primary unique identifier for customer matching.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_email",
        "label":        "Shopify Email",
        "fieldtype":    "Data",
        "insert_after": "shopify_phone",
        "read_only":    1,
    })


# ── Sales Order custom fields ──────────────────────────────────────────────────

def create_sales_order_custom_fields():
    """
    Add collapsible Shopify reference section to Sales Order.

    Placed in More Info → Additional Info (after 'campaign' or nearest stable field).
    unique is NOT set on shopify_order_id — uniqueness is enforced in code,
    excluding cancelled orders, so cancel-and-amend workflows are not blocked.
    shopify_order_name is not created — the value already lives in po_no.
    """
    doctype = "Sales Order"
    anchor  = _so_shopify_anchor()

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_section",
        "label":        "Shopify",
        "fieldtype":    "Section Break",
        "insert_after": anchor,
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_order_id",
        "label":        "Shopify Order ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_section",
        "read_only":    1,
        "description":  "Numeric Shopify order ID. Used for duplicate detection (cancelled orders excluded).",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_store",
        "label":        "Shopify Store",
        "fieldtype":    "Data",
        "insert_after": "shopify_order_id",
        "read_only":    1,
        "description":  "Shop domain e.g. notdrones.myshopify.com.",
    })


# ── Delivery Note custom fields ──────────────────────────────────────────────────

def create_delivery_note_custom_fields():
    """
    Add collapsible Shopify reference section to Delivery Note.
    This allows fields to map from Sales Order -> Delivery Note automatically,
    which is required for list view indicators.
    """
    doctype = "Delivery Note"
    
    # Try to find a good anchor, default to amendment_date
    dn_meta = frappe.get_meta(doctype)
    anchor = "amendment_date"
    for fieldname in ["inter_company_order_reference", "source", "tc_name"]:
        if dn_meta.get_field(fieldname):
            anchor = fieldname
            break

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_section",
        "label":        "Shopify",
        "fieldtype":    "Section Break",
        "insert_after": anchor,
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_order_id",
        "label":        "Shopify Order ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_section",
        "read_only":    1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_store",
        "label":        "Shopify Store",
        "fieldtype":    "Data",
        "insert_after": "shopify_order_id",
        "read_only":    1,
    })


# ── Payment Entry custom fields ────────────────────────────────────────────────

def _section_boundary_anchor(doctype: str, preferred: list, fallback: str) -> str:
    """
    An insert_after anchor that will NOT split an existing section.

    A Section Break inserted mid-section reparents every following field until
    the next break, so standard fields end up rendered inside our collapsible
    section and disappear from where they belong.  Having picked the first
    available field from `preferred`, walk forward to the last field before the
    next Section/Tab Break: our section then begins exactly on a section
    boundary and no existing field moves.

    :param preferred: candidate anchors, most-preferred first
    :param fallback:  used when none of the candidates exist on the doctype
    """
    meta = frappe.get_meta(doctype)

    anchor = ""
    for fieldname in preferred:
        if meta.get_field(fieldname):
            anchor = fieldname
            break
    if not anchor:
        return fallback

    fields = [df.fieldname for df in meta.fields]
    types  = {df.fieldname: df.fieldtype for df in meta.fields}
    try:
        idx = fields.index(anchor)
    except ValueError:
        return anchor

    for i in range(idx + 1, len(fields)):
        if types.get(fields[i]) in ("Section Break", "Tab Break"):
            return fields[i - 1]
        anchor = fields[i]

    return anchor


def _pe_shopify_anchor() -> str:
    """
    Anchor for the Shopify section in Payment Entry.

    Lands near the existing reference fields (Cheque/Reference No + Date),
    because the gateway reference is read alongside them during reconciliation,
    then moves to the section boundary so nothing is reparented.
    """
    return _section_boundary_anchor(
        "Payment Entry",
        ["reference_date", "reference_no", "clearance_date", "remarks"],
        "remarks",  # present on every ERPNext version
    )


def create_payment_entry_custom_fields():
    """
    Add the Shopify gateway-reference fields to Payment Entry.

    custom_gateway_reference holds the payment gateway's own transaction id
    (PayU txnid, Razorpay payment id, …), pulled from the Shopify order's
    transactions after the Payment Entry is created.  It exists so gateway
    settlement reports can be reconciled against ERPNext orders.

    This is deliberately separate from the standard `reference_no`, which this
    integration fills with the Shopify order name (#6282) and which other code
    depends on — nothing here touches it.

    allow_on_submit is set on both fields: Payment Entries are auto-submitted by
    this app, so the reference is written after submission.  read_only keeps
    them out of users' hands — the values are machine-owned.
    """
    doctype = "Payment Entry"
    anchor  = _pe_shopify_anchor()

    create_or_update_custom_field(doctype, {
        "fieldname":    "custom_shopify_gateway_section",
        "label":        "Shopify Payment Gateway",
        "fieldtype":    "Section Break",
        "insert_after": anchor,
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_gateway_reference",
        "label":           "Gateway Payment Reference",
        "fieldtype":       "Data",
        "insert_after":    "custom_shopify_gateway_section",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "The payment gateway's own transaction id (e.g. PayU txnid), read from the Shopify order's transactions. Use this to reconcile gateway settlements against orders. Blank means the gateway returned no reference.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_gateway_column_break",
        "fieldtype":       "Column Break",
        "insert_after":    "custom_gateway_reference",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_gateway_name",
        "label":           "Payment Gateway",
        "fieldtype":       "Data",
        "insert_after":    "custom_gateway_column_break",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "Gateway reported by Shopify for this transaction, e.g. 'Cards, UPI, NB by PayU India'. Identifies which settlement portal the reference belongs to.",
    })


# ── Sales Order Item custom fields ────────────────────────────────────────────

def create_sales_order_item_custom_fields():
    """
    Store the Shopify line item id on each Sales Order Item.

    Needed to fulfil orders accurately.  One Shopify order can carry the same
    SKU on two separate line items — different discounts, different line
    properties — so SKU alone cannot tell a fulfillment which line to ship.  The
    chain used at fulfillment time is:

        Delivery Note Item.so_detail → Sales Order Item → this field
            → FulfillmentOrderLineItem.lineItem.id

    Created and populated even while fulfillment is disabled.  That is
    deliberate: the value can only be captured at sync time, so accumulating it
    now means the feature works properly from day one if it is ever switched on.
    Orders synced without it fall back to SKU matching, which is correct only
    while a SKU appears once on the order.
    """
    create_or_update_custom_field("Sales Order Item", {
        "fieldname":    "custom_shopify_line_item_id",
        "label":        "Shopify Line Item ID",
        "fieldtype":    "Data",
        "insert_after": "item_code",
        "read_only":    1,
        "hidden":       1,   # machine-only; no reason to occupy grid space
        "description":  "Shopify line_item.id for this row. Used to match Delivery Note lines to Shopify fulfillment order lines.",
    })


# ── Delivery Note fulfillment custom fields ───────────────────────────────────

def _dn_fulfillment_anchor() -> str:
    """
    Anchor for the fulfillment section on Delivery Note.

    Prefers to sit just after the existing Shopify section so all Shopify state
    is together, but goes through _section_boundary_anchor: returning
    `shopify_store` directly would insert a Section Break in the middle of that
    section and reparent every standard Delivery Note field that follows it.
    """
    return _section_boundary_anchor(
        "Delivery Note",
        ["shopify_store", "shopify_order_id", "shopify_section",
         "inter_company_order_reference", "source", "tc_name"],
        "amendment_date",
    )


def create_delivery_note_fulfillment_custom_fields():
    """
    Add Shopify fulfillment state to Delivery Note.

    custom_shopify_fulfillment_id is the idempotency key: set means Shopify has
    been told this shipped, and every trigger path (on_submit, scheduler, form
    button, bulk action) treats it as a hard stop.

    All fields are read_only + allow_on_submit: they are written after the
    Delivery Note is submitted, by machine, via frappe.db.set_value.  no_copy
    keeps them off amended copies — an amended Delivery Note has not been
    fulfilled, and inheriting the id would make it permanently unfulfillable.

    The fields are created even while fulfillment is disabled.  They sit in a
    collapsed section and stay blank; nothing reads or writes them until a store
    turns the feature on.
    """
    doctype = "Delivery Note"
    anchor  = _dn_fulfillment_anchor()

    create_or_update_custom_field(doctype, {
        "fieldname":    "custom_shopify_fulfillment_section",
        "label":        "Shopify Fulfillment",
        "fieldtype":    "Section Break",
        "insert_after": anchor,
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_shopify_fulfillment_status",
        "label":           "Fulfillment Status",
        "fieldtype":       "Select",
        "options":         "\nPending\nFulfilled\nPartially Fulfilled\nFailed\nCancelled\nNot Applicable",
        "insert_after":    "custom_shopify_fulfillment_section",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "in_standard_filter": 1,
        "description":     "Pending = claimed by a worker. Fulfilled = Shopify was told. Failed = retried by the hourly scheduler and by the Fulfil button.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_shopify_fulfillment_id",
        "label":           "Shopify Fulfillment ID",
        "fieldtype":       "Data",
        "insert_after":    "custom_shopify_fulfillment_status",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "Set once Shopify has accepted the fulfillment. While this is set, no further fulfillment request is ever sent for this Delivery Note.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_shopify_fulfillment_column_break",
        "fieldtype":       "Column Break",
        "insert_after":    "custom_shopify_fulfillment_id",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_shopify_fulfilled_at",
        "label":           "Fulfilled At",
        "fieldtype":       "Datetime",
        "insert_after":    "custom_shopify_fulfillment_column_break",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "When Shopify accepted the fulfillment. On a Pending or Failed row this is the last attempt time — it doubles as the worker claim stamp.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "custom_shopify_fulfillment_error",
        "label":           "Fulfillment Note / Error",
        "fieldtype":       "Small Text",
        "insert_after":    "custom_shopify_fulfilled_at",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "Why the last attempt failed, or what was left unfulfilled on a partial fulfillment.",
    })


def _refund_request_anchor() -> str:
    """
    Anchor for the write-back section on Refund Request.

    amended_from is the doctype's last field, so a Section Break after it opens
    a new section at the very end of the form and reparents nothing — no need
    for the _section_boundary_anchor walk the Delivery Note and Sales Order
    sections need.  The fallbacks cover a payment_portals version that has
    reordered its own fields.
    """
    meta = frappe.get_meta("Refund Request")
    for fieldname in ["amended_from", "error_log", "confirmed_transaction", "status"]:
        if meta.get_field(fieldname):
            return fieldname
    return "status"


def create_refund_request_writeback_custom_fields():
    """
    Add Shopify refund write-back state to Refund Request.

    Refund Request belongs to payment_portals.  These are Custom Fields, so they
    live in their own table rather than in refund_request.json: nothing in that
    app is edited, and a `bench migrate` there will not undo them.  This app
    already extends five DocTypes it does not own the same way.

    shopify_refund_gid is the idempotency key: set means Shopify has been told,
    and every trigger path treats it as a hard stop.  It is also what the
    credit-note webhook guard matches against, so our own write-back cannot come
    back round as a second Credit Note.

    All fields are read_only + allow_on_submit: they are written after the
    Refund Request is submitted, by machine, via frappe.db.set_value.  no_copy
    keeps them off amended copies — an amended refund has not been written back,
    and inheriting the GID would make it permanently unwritable, silently.

    Created even while the write-back is disabled: they sit in a collapsed
    section and stay blank until a store turns the feature on.  Skipped entirely
    when payment_portals is absent, so this app stays installable without it.
    """
    doctype = "Refund Request"
    if not frappe.db.exists("DocType", doctype):
        return  # payment_portals not installed on this site

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_refund_writeback_section",
        "label":        "Shopify Refund Write-Back",
        "fieldtype":    "Section Break",
        "insert_after": _refund_request_anchor(),
        "collapsible":  1,
        "description":  "What this app told Shopify about this refund. Populated automatically once the refund is Completed, and only for refunds against a Shopify order. A <b>Done</b> here is a payment instruction Shopify accepted, not merely a record of one.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "shopify_writeback_status",
        "label":           "Shopify Write-Back Status",
        "fieldtype":       "Select",
        "options":         "\nPending\nDone\nFailed\nSkipped\nUnverified",
        "insert_after":    "shopify_refund_writeback_section",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "in_standard_filter": 1,
        "description":     "Pending = claimed by a worker. Done = Shopify accepted the refund. Failed = nothing was sent, safe to retry with the Refund in Shopify button. Skipped = nothing to send (not a Shopify order, or the refund came from Shopify already). <b>Unverified = the request reached Shopify and the outcome could not be confirmed, so the customer may already have been paid</b> — do not retry; check the order in Shopify and record what you find.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "shopify_refund_gid",
        "label":           "Shopify Refund ID",
        "fieldtype":       "Data",
        "insert_after":    "shopify_writeback_status",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "Set once Shopify has accepted the refund. While this is set, no further refund is ever sent for this document.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_refund_writeback_column_break",
        "fieldtype":    "Column Break",
        "insert_after": "shopify_refund_gid",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "shopify_refund_gateway",
        "label":           "Shopify Refund Gateway",
        "fieldtype":       "Data",
        "insert_after":    "shopify_refund_writeback_column_break",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "The gateway Shopify attached the refund to, copied verbatim from the order's own parent transaction. On orders created by the Cashfree-OCC app this reads <b>manual</b>, because Shopify holds no gateway transaction of its own — that does <b>not</b> mean the customer went unpaid. The OCC app turns the Shopify refund into a real Cashfree refund; the proof is the Cashfree refund that follows, which Settlement Recon ingests at a median lag of about 48 hours. Its absence proves nothing for a day or two.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "shopify_writeback_at",
        "label":           "Shopify Write-Back At",
        "fieldtype":       "Datetime",
        "insert_after":    "shopify_refund_gateway",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "When Shopify accepted the refund. On a Pending or Failed row this is the last attempt time — it doubles as the worker claim stamp.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":       "shopify_writeback_error",
        "label":           "Shopify Write-Back Error",
        "fieldtype":       "Small Text",
        "insert_after":    "shopify_writeback_at",
        "read_only":       1,
        "allow_on_submit": 1,
        "no_copy":         1,
        "description":     "Why the last attempt failed, or why it was skipped.",
    })
