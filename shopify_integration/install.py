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

def _pe_shopify_anchor() -> str:
    """
    Find a safe insert_after anchor for the Shopify section in Payment Entry.

    Two requirements:

      1. Land near the existing reference fields (Cheque/Reference No + Date),
         because the gateway reference is read alongside them during
         reconciliation.

      2. Do NOT split an existing section.  A Section Break inserted mid-section
         reparents every following field until the next break — so once a
         preferred anchor is found, we walk forward to the last field before the
         next Section/Tab Break.  Our section then begins exactly on a section
         boundary and no ERPNext field moves.
    """
    meta = frappe.get_meta("Payment Entry")

    anchor = ""
    for fieldname in ["reference_date", "reference_no", "clearance_date", "remarks"]:
        if meta.get_field(fieldname):
            anchor = fieldname
            break
    if not anchor:
        return "remarks"  # last resort; present on every ERPNext version

    fields = [df.fieldname for df in meta.fields]
    types  = {df.fieldname: df.fieldtype for df in meta.fields}
    try:
        idx = fields.index(anchor)
    except ValueError:
        return anchor

    # Walk forward to the field just before the next Section/Tab Break.
    for i in range(idx + 1, len(fields)):
        if types.get(fields[i]) in ("Section Break", "Tab Break"):
            return fields[i - 1]
        anchor = fields[i]

    return anchor


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
    Insert the fulfillment section after the existing Shopify section on Delivery
    Note when it is there, so all Shopify state sits together.  Falls back the
    same way create_delivery_note_custom_fields() does.
    """
    meta = frappe.get_meta("Delivery Note")
    for fieldname in ["shopify_store", "shopify_order_id", "shopify_section",
                      "inter_company_order_reference", "source", "tc_name"]:
        if meta.get_field(fieldname):
            return fieldname
    return "amendment_date"


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
