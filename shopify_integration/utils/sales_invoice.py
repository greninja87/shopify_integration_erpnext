"""
sales_invoice.py — Create ERPNext Sales Invoice from Shopify-generated documents.

Two entry points:

  create_sales_invoice_from_so(so, settings)
      Used by the "After Payment Entry" flow (Option B).
      Creates the SI directly from the submitted Sales Order — no Delivery Note
      required.  Called from sales_order.py immediately after a successful PE.

  create_sales_invoice_from_dn(dn_name, settings)
      Used by the scheduler for the "After Delivery Note" flow (Option A).
      Creates the SI from a submitted Delivery Note so stock movement and
      billing are properly linked.  Called from scheduler.py.

Both functions raise on error so the caller can log appropriately.
"""

import frappe


def create_sales_invoice_from_so(so, settings, pe_name: str = None) -> str:
    """
    Create a Sales Invoice directly from a submitted Sales Order.

    :param so:       Submitted ERPNext Sales Order document
    :param settings: Shopify Settings document
    :return:         Sales Invoice name
    :raises:         Any exception from ERPNext SI creation (caller logs it)
    """
    from erpnext.selling.doctype.sales_order.sales_order import (
        make_sales_invoice as so_to_si,
    )

    # ERPNext's make_sales_invoice calls get_mapped_doc which calls
    # check_permission() before ignore_permissions is applied.  In a webhook
    # context frappe.session.user may be Guest.  Swap to Administrator for
    # the duration of this call (same pattern as set_missing_values in SO).
    _prev_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        si = so_to_si(so.name)
    finally:
        frappe.session.user = _prev_user

    # Copy payment terms from the Sales Order (ERPNext's mapper may skip this
    # in some versions — explicit copy guarantees consistency).
    if so.get("payment_terms_template"):
        si.payment_terms_template = so.payment_terms_template
    if so.get("payment_schedule"):
        si.payment_schedule = []  # let ERPNext regenerate from template

    if settings.get("cost_center"):
        si.cost_center = settings.cost_center
        for item in si.items:
            item.cost_center = settings.cost_center

    if settings.get("si_naming_series"):
        si.naming_series = settings.si_naming_series

    # Ensure grand_total is computed before advance allocation.
    # make_sales_invoice calls calculate_taxes_and_totals internally, but
    # running it again after cost_center overrides is cheap and safe.
    si.run_method("calculate_taxes_and_totals")

    # Always enable FIFO advance allocation so any Payment Entry
    # (whether created by our integration or manually) is automatically
    # linked to this Sales Invoice on submit.
    si.allocate_advances_automatically = 1

    si.flags.ignore_permissions = True
    si.insert()

    if settings.get("auto_submit_sales_invoice"):
        si.flags.ignore_permissions = True
        si.submit()

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; SI must persist for advance allocation
    return si.name


def create_sales_invoice_from_dn(dn_name: str, settings) -> str:
    """
    Create a Sales Invoice from a submitted Delivery Note.

    :param dn_name:  Delivery Note document name
    :param settings: Shopify Settings document
    :return:         Sales Invoice name
    :raises:         Any exception from ERPNext SI creation (caller logs it)
    """
    from erpnext.stock.doctype.delivery_note.delivery_note import (
        make_sales_invoice as dn_to_si,
    )

    _prev_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        si = dn_to_si(dn_name)
    finally:
        frappe.session.user = _prev_user

    if settings.get("cost_center"):
        si.cost_center = settings.cost_center
        for item in si.items:
            item.cost_center = settings.cost_center

    if settings.get("si_naming_series"):
        si.naming_series = settings.si_naming_series

    # Always enable FIFO advance allocation so any Payment Entry
    # linked to the Sales Order is automatically allocated to this SI.
    si.allocate_advances_automatically = 1

    si.flags.ignore_permissions = True
    si.insert()

    if settings.get("auto_submit_sales_invoice"):
        si.flags.ignore_permissions = True
        si.submit()

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in scheduler job; SI must persist independently
    return si.name
