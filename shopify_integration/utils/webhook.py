"""
webhook.py — Shopify Log helpers.

Logging policy:
  * EVERY incoming Shopify webhook creates a Shopify Log entry.
  * The log tracks the webhook's lifecycle via its `status` field:
        Received  -> on arrival
        Processed -> Sales Order created successfully
        Failed    -> processing threw an exception
        Skipped   -> unknown store, unhandled topic, or duplicate
  * The ERPNext Error Log is reserved for real exceptions — not for
    successful SO creation.
"""

import json
import frappe


def log_webhook(
    topic: str,
    shop_domain: str,
    order_data: dict,
    status: str = "Received",
    error_message: str = "",
    so_name: str = "",
) -> str:
    """
    Create a Shopify Log entry for this webhook event.

    :param topic:         Shopify webhook topic header (e.g. orders/create)
    :param shop_domain:   Shopify shop domain header
    :param order_data:    Parsed webhook payload (dict)
    :param status:        Received | Processed | Failed | Skipped
    :param error_message: Error text (Failed) or skip reason (Skipped)
    :param so_name:       ERPNext Sales Order name, if already known
    :return:              Document name of the created log, or empty string on error
    """
    try:
        log = frappe.get_doc({
            "doctype":             "Shopify Log",
            "topic":               topic,
            "shop_domain":         shop_domain,
            "shopify_order_id":    str(order_data.get("id", "")) if isinstance(order_data, dict) else "",
            "shopify_order_name":  order_data.get("name", "") if isinstance(order_data, dict) else "",
            "status":              status,
            "payload":             json.dumps(order_data, indent=2, default=str),
            "error_message":       (error_message or "")[:5000],
            "erpnext_sales_order": so_name or "",
        })
        log.flags.ignore_permissions = True
        log.insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — log insert runs in background job outside request lifecycle
        return log.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Log Creation Failed")
        return ""


def update_log_status(
    log_name: str,
    shopify_order_id: str,
    status: str,
    so_name: str = "",
    error: str = "",
):
    """
    Update an existing Shopify Log entry after processing completes.

    :param log_name:         Document name returned from log_webhook()
    :param shopify_order_id: Shopify order ID (fallback lookup if log_name is empty)
    :param status:           Received | Processed | Failed | Skipped
    :param so_name:          ERPNext Sales Order name on success
    :param error:            Error message on failure / skip reason
    """
    try:
        name = log_name
        if not name and shopify_order_id:
            name = frappe.db.get_value(
                "Shopify Log", {"shopify_order_id": shopify_order_id}, "name"
            )
        if name:
            updates = {"status": status}
            if so_name:
                updates["erpnext_sales_order"] = so_name
            if error:
                updates["error_message"] = error[:5000]
            frappe.db.set_value("Shopify Log", name, updates)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Log Status Update Failed")


# The retry flow lives on the Shopify Log doctype controller
# (shopify_integration/doctype/shopify_log/shopify_log.py → retry_order).
