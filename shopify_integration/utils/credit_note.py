"""
credit_note.py — Create ERPNext Credit Note (return Sales Invoice) for Shopify refunds.

Entry points:

  _create_credit_note_background(refund_data, store_name, log_name)
      Background job enqueued by api.py when a refunds/create webhook arrives
      and credit_note_creation == "Auto".  Updates the Shopify Log on completion.

  create_credit_note_from_shopify_refund(refund_data, settings) -> str
      Core logic: finds the original Sales Invoice, checks idempotency, builds
      the return document, applies naming series / cost center, and inserts /
      submits it.

  find_sales_invoice_for_order(shopify_order_id) -> str | None
      Looks up the active (submitted, non-return) Sales Invoice that was created
      for a given Shopify order ID.  Used to locate what to return against.
"""

import frappe


# ── Background job (enqueued from api.py) ─────────────────────────────────────

def _create_credit_note_background(refund_data: dict, store_name: str, log_name: str = ""):
    """Background job: create a credit note and update the Shopify Log."""
    from shopify_integration.utils.webhook import update_log_status

    settings = frappe.get_doc("Shopify Settings", store_name)
    order_id = str(refund_data.get("order_id", ""))

    try:
        cn_name = create_credit_note_from_shopify_refund(refund_data, settings)
        update_log_status(
            log_name=log_name,
            shopify_order_id=order_id,
            status="Processed",
            error=f"Credit Note {cn_name} created.",
        )
        frappe.logger().info(
            f"Shopify: Credit Note {cn_name} created for Shopify order {order_id}"
        )
    except Exception:
        tb = frappe.get_traceback()
        frappe.log_error(tb, f"Shopify: Credit Note Failed — order {order_id}")
        update_log_status(
            log_name=log_name,
            shopify_order_id=order_id,
            status="Failed",
            error=tb[:5000],
        )


# ── Core credit note creation ──────────────────────────────────────────────────

def create_credit_note_from_shopify_refund(refund_data: dict, settings) -> str:
    """
    Create a Credit Note (return Sales Invoice) from a Shopify refund payload.

    Idempotent: if a non-cancelled return SI already exists against the same
    original Sales Invoice, returns its name without creating a duplicate.

    :param refund_data: Shopify refund dict (from refunds/create webhook payload)
    :param settings:    Shopify Settings document
    :return:            Credit Note (Sales Invoice) name
    :raises:            frappe.DoesNotExistError when no SI is found for the order
    :raises:            Any exception from ERPNext document creation
    """
    from erpnext.controllers.accounts_controller import make_return_doc

    order_id = str(refund_data.get("order_id", ""))
    si_name = find_sales_invoice_for_order(order_id)
    if not si_name:
        frappe.throw(
            f"No submitted Sales Invoice found for Shopify order {order_id}. "
            "Ensure the Sales Invoice was created (and submitted) before the refund arrives.",
            frappe.DoesNotExistError,
        )

    # Idempotency: if a return SI already exists against this SI, skip creation.
    existing_cn = frappe.db.get_value(
        "Sales Invoice",
        {
            "return_against": si_name,
            "docstatus": ["!=", 2],
            "is_return": 1,
        },
        "name",
    )
    if existing_cn:
        frappe.logger().info(
            f"Shopify: Credit Note {existing_cn} already exists for SI {si_name} "
            f"(Shopify order {order_id}) — skipping duplicate creation."
        )
        return existing_cn

    # Build the return document.  ERPNext's make_return_doc copies the original
    # SI and flips quantities/amounts to negative, sets is_return=1, and links
    # return_against to the original.
    _prev_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        cn = make_return_doc("Sales Invoice", si_name)
    finally:
        frappe.session.user = _prev_user

    if settings.get("cn_naming_series"):
        cn.naming_series = settings.cn_naming_series

    if settings.get("cost_center"):
        cn.cost_center = settings.cost_center
        for item in cn.items:
            item.cost_center = settings.cost_center

    cn.flags.ignore_permissions = True
    cn.insert()

    if settings.get("auto_submit_credit_note"):
        cn.flags.ignore_permissions = True
        cn.submit()

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; CN must persist independently
    return cn.name


# ── Lookup helper ──────────────────────────────────────────────────────────────

def find_sales_invoice_for_order(shopify_order_id: str):
    """
    Find the most recent submitted, non-return Sales Invoice linked to the
    given Shopify order ID.  Returns the SI name, or None if not found.

    Lookup path: Shopify order ID → Sales Order → Sales Invoice Item → SI.
    """
    so_name = frappe.db.get_value(
        "Sales Order",
        {"shopify_order_id": shopify_order_id, "docstatus": 1},
        "name",
    )
    if not so_name:
        return None

    result = frappe.db.sql(
        """
        SELECT si.name
        FROM   `tabSales Invoice Item` sii
        JOIN   `tabSales Invoice` si ON si.name = sii.parent
        WHERE  sii.sales_order = %s
          AND  si.docstatus    = 1
          AND  si.is_return    = 0
        ORDER  BY si.creation DESC
        LIMIT  1
        """,
        so_name,
        as_list=True,
    )
    return result[0][0] if result else None
