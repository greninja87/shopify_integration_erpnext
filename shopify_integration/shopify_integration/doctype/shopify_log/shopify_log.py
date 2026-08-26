"""
shopify_log.py — Controller for Shopify Log DocType.

Actions on the log form:

  Retry Order              replay the payload through Sales Order creation
  Re-fetch from Shopify    pull the order fresh from the Admin API
  Edit Payload             hand-correct the payload before retrying
  Reset For Retry          clear the SO link so a retry is allowed again

Payload correction
------------------
Some webhooks arrive with data that cannot produce a valid Sales Order — most
commonly a wrong or missing pincode, which India Compliance rejects because it
cannot reconcile the PIN against the GST state.  Retrying such a log replays the
same bad data and fails the same way, so the payload has to be corrected first.

`payload` is deliberately read-only and is NEVER modified: it is the record of
what Shopify actually sent, and Frappe rightly refuses to let Customize Form
unset read-only on it.  Corrections go into `corrected_payload` instead, and
retry prefers that when present (see get_effective_payload).  So a corrected log
shows both versions — what arrived, and what was replayed, with who changed it
and why.

Two ways to correct:

  Re-fetch from Shopify   authoritative.  Fix the order in Shopify (which is
                          where the customer's own record lives), then pull it
                          back.  Preferred for address problems: the correction
                          also fixes the source for the customer's next order.

  Edit Payload            manual JSON edit, for cases where Shopify itself
                          cannot hold the corrected value.

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
from frappe.utils import now_datetime


class ShopifyLog(Document):
    pass


# ── Payload resolution ────────────────────────────────────────────────────────

def get_effective_payload(log) -> str:
    """
    The payload a retry should actually replay.

    corrected_payload when set, otherwise the original.  One place decides this,
    so no code path can accidentally replay the uncorrected data.
    """
    corrected = (log.get("corrected_payload") or "").strip()
    return corrected or (log.get("payload") or "")


def _parse_payload(raw: str, label: str = "Payload") -> dict:
    """Parse a payload string into a dict, with a message worth reading."""
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        frappe.throw(f"{label} is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        frappe.throw(f"{label} must be a JSON object, got {type(parsed).__name__}.")
    return parsed


def _assert_same_order(log, parsed: dict):
    """
    A corrected payload must still describe the SAME Shopify order.

    Without this, a typo in the `id` field would quietly attach the retry to a
    different order — creating a Sales Order against the wrong customer while
    the log still claims to be about this one.
    """
    log_order_id = str(log.get("shopify_order_id") or "").strip()
    new_order_id = str(parsed.get("id") or "").strip()

    if not log_order_id:
        return  # nothing to compare against; the retry's own checks still apply

    if not new_order_id:
        # Dropping `id` is not harmless: create_sales_order_from_shopify reads it
        # to stamp shopify_order_id on the Sales Order, which is what duplicate
        # detection relies on.  A payload without it would create an SO that no
        # future webhook can recognise.
        frappe.throw(
            f"The corrected payload has no <code>id</code> field. Shopify order "
            f"{log_order_id} must keep its id — it is what links the Sales Order "
            f"back to Shopify and prevents duplicates."
        )

    if log_order_id != new_order_id:
        frappe.throw(
            f"The payload's order id ({new_order_id}) does not match this log's "
            f"Shopify Order ID ({log_order_id}). Correcting a payload must not "
            f"change which order it belongs to."
        )


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

    raw_payload = get_effective_payload(log)
    if not raw_payload:
        frappe.throw("No payload stored in this log entry. Cannot retry.")

    if log.status == "Processed" and log.erpnext_sales_order:
        frappe.throw(
            f"This webhook has already been processed into Sales Order "
            f"{log.erpnext_sales_order}. Delete or cancel that SO first if "
            f"you want to retry."
        )

    using_correction = bool((log.get("corrected_payload") or "").strip())
    order_data = _parse_payload(
        raw_payload,
        "Corrected Payload" if using_correction else "Payload",
    )

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
    # it back to the log and bail — full SO+PE+SI re-creation is only meaningful
    # when the target SO is gone.
    #
    # NOTE — PE-only failure scenario:
    #   If the SO was created successfully but the Payment Entry failed (e.g.
    #   the "Difference Amount must be zero" bug fixed in payment_entry.py),
    #   this check will find the existing SO and return "duplicate".  The PE
    #   will NOT be retried automatically.
    #   To retry: cancel/delete the SO first (use the Shopify Log →
    #   Reset for Retry button, then cancel the linked SO and resubmit the log).
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

        # Send failure email — settings may be None if store lookup failed above
        if settings:
            try:
                from shopify_integration.utils.sales_order import send_failure_email
                send_failure_email(settings, order_data, f"{error_msg}\n\n{traceback}")
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Shopify: Failure Email Send Error")

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


# ── Payload correction actions ────────────────────────────────────────────────

def _require_write(docname: str):
    """
    Correcting a payload changes what a retry will create, so it needs write
    permission on the log — not merely read.
    """
    frappe.has_permission("Shopify Log", "write", doc=docname, throw=True)


def _store_correction(docname: str, payload_json: str, reason: str, source: str) -> dict:
    """
    Write a corrected payload with full attribution.  Shared by the manual edit
    and the Shopify re-fetch so both are recorded identically.
    """
    log = frappe.get_doc("Shopify Log", docname)

    parsed = _parse_payload(payload_json, "Corrected Payload")
    _assert_same_order(log, parsed)

    # Re-serialise from the parsed object: guarantees the stored value is valid
    # JSON and readable, whatever whitespace the caller sent.
    normalised = json.dumps(parsed, indent=2, ensure_ascii=False)

    frappe.db.set_value("Shopify Log", docname, {
        "corrected_payload":         normalised,
        "correction_reason":         (reason or "").strip()[:500],
        "corrected_by":              frappe.session.user,
        "corrected_at":              now_datetime(),
        "payload_correction_status": source,
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — explicit user action; correction must persist before the user retries

    frappe.logger().info(
        f"Shopify Log {docname}: payload corrected via {source} "
        f"by {frappe.session.user}"
    )
    return {"status": "ok", "docname": docname, "source": source}


@frappe.whitelist()
def save_corrected_payload(docname: str, payload_json: str, reason: str = "") -> dict:
    """
    Store a hand-corrected payload for this log.  Called by the Edit Payload dialog.

    The original `payload` is left untouched.  Retry Order will use this instead
    (see get_effective_payload), so the fix takes effect without destroying the
    record of what Shopify actually sent.

    :raises frappe.ValidationError: invalid JSON, or an order id that does not
                                    match this log
    """
    _require_write(docname)

    if not (payload_json or "").strip():
        frappe.throw("Corrected payload is empty. Nothing to save.")

    return _store_correction(
        docname, payload_json, reason or "Manually corrected.", "Manually edited"
    )


@frappe.whitelist()
def refetch_payload_from_shopify(docname: str, reason: str = "") -> dict:
    """
    Pull this order fresh from the Shopify Admin API and store it as the
    corrected payload.

    The intended workflow for bad address data: fix the order in Shopify first —
    that is where the customer's own record lives, so the correction also fixes
    the source for their next order — then re-fetch here and retry.

    Requires an Admin API access token on the store
    (Shopify Settings → Connection → Shopify Admin API).
    """
    _require_write(docname)

    from shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings import (
        get_settings_for_store,
    )
    from shopify_integration.utils.shopify_api import (
        ShopifyAPIError,
        get_order,
        has_admin_api_credentials,
    )

    log = frappe.get_doc("Shopify Log", docname)

    order_id = str(log.get("shopify_order_id") or "").strip()
    if not order_id:
        # Fall back to the payload's own id — refund logs and early-failure logs
        # do not always have the column populated.
        raw = get_effective_payload(log)
        if raw:
            try:
                order_id = str((json.loads(raw) or {}).get("id") or "").strip()
            except Exception:
                order_id = ""
    if not order_id:
        frappe.throw(
            "This log has no Shopify order id, so there is nothing to re-fetch. "
            "Use Edit Payload instead."
        )

    settings = get_settings_for_store(log.get("shop_domain") or "")
    if not settings:
        frappe.throw(
            f"No active Shopify Settings found for store "
            f"'{log.get('shop_domain') or ''}'. Check the store is configured "
            f"and Enable Sync is on."
        )

    if not has_admin_api_credentials(settings):
        frappe.throw(
            "Re-fetching needs an <b>Admin API Access Token</b> for this store "
            "(Shopify Settings → Connection → Shopify Admin API). "
            "Without it, use <b>Edit Payload</b> to correct the data by hand."
        )

    try:
        order = get_order(settings, order_id)
    except ShopifyAPIError as exc:
        frappe.throw(f"Could not fetch order {order_id} from Shopify: {exc}")

    return _store_correction(
        docname,
        json.dumps(order, ensure_ascii=False),
        reason or f"Re-fetched from Shopify on {now_datetime():%Y-%m-%d %H:%M}.",
        "Re-fetched from Shopify",
    )


@frappe.whitelist()
def clear_corrected_payload(docname: str) -> dict:
    """
    Discard the correction and go back to replaying exactly what Shopify sent.
    """
    _require_write(docname)

    if not frappe.db.exists("Shopify Log", docname):
        frappe.throw(f"Shopify Log '{docname}' not found.")

    frappe.db.set_value("Shopify Log", docname, {
        "corrected_payload":         "",
        "correction_reason":         "",
        "corrected_by":              None,
        "corrected_at":              None,
        "payload_correction_status": "",
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — explicit user action; must persist immediately
    return {"status": "ok", "docname": docname}


@frappe.whitelist()
def get_payload_for_edit(docname: str) -> dict:
    """
    Payload text for the Edit Payload dialog, pretty-printed so a human can find
    the field they need to change.

    Returns the correction when one exists (so repeated edits build on each
    other), otherwise the original.
    """
    frappe.has_permission("Shopify Log", "read", doc=docname, throw=True)

    log = frappe.get_doc("Shopify Log", docname)
    raw = get_effective_payload(log)

    pretty = raw
    if raw:
        try:
            pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            pass  # not valid JSON — hand it back as-is so it can be repaired

    return {
        "payload": pretty,
        "is_corrected": bool((log.get("corrected_payload") or "").strip()),
        "correction_reason": log.get("correction_reason") or "",
        "shopify_order_id": log.get("shopify_order_id") or "",
    }
