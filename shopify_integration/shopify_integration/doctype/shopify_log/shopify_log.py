"""
shopify_log.py — Controller for Shopify Log DocType.

Provides the "Retry Order" button action that re-processes
a failed / skipped webhook payload.

Logging policy reminder:
  * Every webhook creates a Shopify Log entry (for audit + retry).
  * On successful retry the log's status is set to "Processed" with the
    ERPNext Sales Order linked — the log is retained, not deleted.
  * On failed retry the log is retained with status "Failed" and the new
    error message.
"""

import json
import frappe
from frappe.model.document import Document


class ShopifyLog(Document):
    pass


@frappe.whitelist()
def retry_order(docname: str):
    """
    Re-process a Shopify Log entry by replaying its stored payload through
    create_sales_order_from_shopify().  Called from the Retry Order button.

    Response shape:
      { "status": "success",   "sales_order": "<SO_NAME>" }
      { "status": "duplicate", "sales_order": "<EXISTING_SO_NAME>" }
      (on exception: frappe.throw with the error message)
    """
    log = frappe.get_doc("Shopify Log", docname)

    if not log.payload:
        frappe.throw("No payload stored in this log entry. Cannot retry.")

    if log.status == "Processed" and log.erpnext_sales_order:
        frappe.throw(
            f"This webhook has already been processed into Sales Order "
            f"{log.erpnext_sales_order}. Delete or cancel that SO first if "
            f"you want to retry."
        )

    try:
        order_data = json.loads(log.payload)
    except Exception:
        frappe.throw("Payload is not valid JSON. Cannot retry.")

    # ── Resolve store ─────────────────────────────────────────────────────────
    from shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings import (
        get_settings_for_store,
    )
    from shopify_integration.utils.sales_order import create_sales_order_from_shopify

    shop_domain = log.shop_domain or order_data.get("shop_domain", "")
    settings = get_settings_for_store(shop_domain)
    if not settings:
        frappe.throw(
            f"No active Shopify Settings found for store '{shop_domain}'. "
            "Check that the store is configured and Enable Sync is turned on."
        )

    # ── Pre-flight duplicate check ────────────────────────────────────────────
    # If a live (non-cancelled) SO already exists for this Shopify order, link
    # it back to the log and bail — retry is only meaningful when the target
    # SO is gone.
    shopify_order_id = str(order_data.get("id", "")) or (log.shopify_order_id or "")
    if shopify_order_id:
        existing = frappe.db.get_value(
            "Sales Order",
            {"shopify_order_id": shopify_order_id, "docstatus": ["!=", 2]},
            "name"
        )
        if existing:
            frappe.db.set_value(
                "Shopify Log", docname,
                {
                    "erpnext_sales_order": existing,
                    "status":              "Skipped",
                    "error_message":       f"Live Sales Order {existing} already exists for this Shopify order.",
                },
            )
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; must persist duplicate status before return
            return {"status": "duplicate", "sales_order": existing}

    # ── Permission bypass for SO creation ─────────────────────────────────────
    # set_missing_values() → _get_party_details → frappe.has_permission() checks
    # the *session* user.  A non-admin ERPNext user (Sales User role etc.) may
    # not have read access on Customer, triggering a PermissionError.
    #
    # WHY frappe.flags and NOT frappe.set_user():
    #   frappe.set_user() calls session_obj.update_session() which writes to
    #   Redis IMMEDIATELY — not deferred to request teardown.  This corrupts
    #   the caller's browser session, causing "User None not found",
    #   "getdoc is not whitelisted", and forced logout on the very next page
    #   load, regardless of any try/finally restore attempt.
    #
    #   frappe.flags.ignore_permissions is a plain Python attribute on
    #   frappe.local.flags — entirely request-local, zero Redis involvement,
    #   zero session side effects.  It is the correct Frappe pattern for
    #   system-level operations that need to bypass permission checks.
    _prev_ignore = frappe.flags.ignore_permissions
    so_name = None
    try:
        frappe.flags.ignore_permissions = True

        # ── Replay creation ───────────────────────────────────────────────────
        so_name = create_sales_order_from_shopify(order_data, settings)

    except Exception as e:
        frappe.db.rollback()
        error_msg = str(e)
        traceback  = frappe.get_traceback()
        frappe.db.set_value("Shopify Log", docname, {
            "status":        "Failed",
            "error_message": f"Retry failed: {error_msg}\n\n{traceback}",
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — after rollback; must persist error status in a new transaction
        frappe.throw(f"Retry failed: {error_msg}")

    finally:
        # Restore the original flag value — keeps this function's side effects
        # strictly contained within its own scope.
        frappe.flags.ignore_permissions = _prev_ignore

    # ── Success — keep the log and mark it Processed with the new SO link ───
    frappe.db.set_value("Shopify Log", docname, {
        "status":              "Processed",
        "error_message":       "",
        "erpnext_sales_order": so_name or "",
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; must persist processed status before return
    return {"status": "success", "sales_order": so_name}


@frappe.whitelist()
def reset_log_for_retry(docname: str):
    """
    Clear the Sales Order link and reset the status on a Shopify Log so it's
    ready to be retried.  Useful if you manually deleted the target SO and
    need to force the log back into a retry-eligible state.
    """
    if not frappe.db.exists("Shopify Log", docname):
        frappe.throw(f"Shopify Log '{docname}' not found.")

    frappe.db.set_value(
        "Shopify Log", docname,
        {
            "erpnext_sales_order": "",
            "status":              "Skipped",
            "error_message":       "Manually reset — linked Sales Order was deleted. Ready for retry.",
        },
    )
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — explicit user action; must persist reset status immediately
    return {"status": "ok", "docname": docname}
