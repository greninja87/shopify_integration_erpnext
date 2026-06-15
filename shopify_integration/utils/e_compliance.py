"""
e_compliance.py — Trigger e-Invoice and e-Waybill generation via India Compliance.

Called from sales_invoice.py after a Shopify Sales Invoice is submitted.
Both operations are enqueued as independent background jobs so a portal
error or IRP timeout never blocks or rolls back the Sales Invoice.

Entry point:
  trigger_e_compliance_for_si(si_name, settings)
      Checks the two flags on Shopify Settings and enqueues the relevant jobs.
      No-ops silently when India Compliance is not installed or a flag is off.

Background jobs (called by RQ worker):
  _generate_e_invoice(si_name)
  _generate_e_waybill(si_name)
"""

import frappe


def trigger_e_compliance_for_si(si_name: str, settings) -> None:
    """
    Enqueue e-Invoice and/or e-Waybill generation for a submitted Sales Invoice.

    Uses enqueue_after_commit=True so the SI is fully committed to the database
    before the IRP portal call starts — a portal failure can never roll back the SI.

    :param si_name:  Submitted Sales Invoice name
    :param settings: Shopify Settings document for this store
    """
    if settings.get("enable_e_invoice"):
        frappe.enqueue(
            "shopify_integration.utils.e_compliance._generate_e_invoice",
            si_name=si_name,
            queue="default",
            timeout=120,
            job_name=f"shopify_einvoice_{si_name}",
            enqueue_after_commit=True,
        )

    if settings.get("enable_e_waybill"):
        frappe.enqueue(
            "shopify_integration.utils.e_compliance._generate_e_waybill",
            si_name=si_name,
            queue="default",
            timeout=120,
            job_name=f"shopify_ewaybill_{si_name}",
            enqueue_after_commit=True,
        )


# ── Background jobs ────────────────────────────────────────────────────────────

def _generate_e_invoice(si_name: str) -> None:
    """
    Background job: generate an e-Invoice for a submitted Sales Invoice.

    e-Invoice is only applicable to B2B transactions (customer has a GSTIN).
    We check gst_category on the SI before calling India Compliance so we
    never create an Integration Request log for ineligible B2C invoices.
    """
    si_data = frappe.db.get_value(
        "Sales Invoice", si_name, ["gst_category", "billing_address_gstin"], as_dict=True
    )
    if not si_data:
        return

    # e-Invoice is only for B2B (registered buyers with GSTIN).
    # B2C Large, B2C Small, and Unregistered are all ineligible.
    if (si_data.gst_category or "") not in ("B2B", "SEZ With Payment", "SEZ Without Payment", "Deemed Export"):
        frappe.logger().info(
            f"Shopify: e-Invoice skipped for {si_name} — gst_category is '{si_data.gst_category}' (not B2B)"
        )
        return

    try:
        from india_compliance.gst_india.utils.e_invoice import generate_e_invoice
    except ImportError:
        frappe.log_error(
            "India Compliance app is not installed. "
            "Install it to enable e-Invoice generation via Shopify Integration.",
            f"Shopify: e-Invoice Skipped (app missing) — {si_name}",
        )
        return

    try:
        generate_e_invoice(si_name, throw=False)
        frappe.logger().info(
            f"Shopify: e-Invoice generation triggered for Sales Invoice {si_name}"
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: e-Invoice Generation Failed — {si_name}",
        )


def _generate_e_waybill(si_name: str) -> None:
    """
    Background job: generate an e-Waybill for a submitted Sales Invoice.

    e-Waybill is mandatory when taxable value of goods > ₹50,000 and goods
    are being transported.  It applies to both B2B and B2C transactions above
    that threshold — India Compliance validates eligibility internally.

    We pre-check the grand_total so we don't hit the IRP portal for small
    invoices that are clearly below the ₹50,000 threshold.
    """
    si_data = frappe.db.get_value(
        "Sales Invoice", si_name, ["grand_total", "currency"], as_dict=True
    )
    if not si_data:
        return

    # Skip IRP call for invoices clearly below the ₹50,000 threshold.
    # India Compliance will still enforce the exact taxable-value check;
    # this is just a cheap early exit for small Shopify orders.
    if (si_data.currency or "INR") == "INR" and (si_data.grand_total or 0) < 50000:
        frappe.logger().info(
            f"Shopify: e-Waybill skipped for {si_name} — grand_total "
            f"₹{si_data.grand_total} is below ₹50,000 threshold"
        )
        return

    try:
        from india_compliance.gst_india.utils.e_waybill import generate_e_waybill
    except ImportError:
        frappe.log_error(
            "India Compliance app is not installed. "
            "Install it to enable e-Waybill generation via Shopify Integration.",
            f"Shopify: e-Waybill Skipped (app missing) — {si_name}",
        )
        return

    try:
        generate_e_waybill(doctype="Sales Invoice", docname=si_name)
        frappe.logger().info(
            f"Shopify: e-Waybill generation triggered for Sales Invoice {si_name}"
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: e-Waybill Generation Failed — {si_name}",
        )
