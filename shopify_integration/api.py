"""
api.py — Shopify Webhook Receiver
Endpoint: /api/method/shopify_integration.api.shopify_webhook

Register this URL in Shopify Admin → Settings → Notifications → Webhooks
  → https://your-domain/api/method/shopify_integration.api.shopify_webhook

Logging policy:
  * EVERY incoming webhook creates a Shopify Log entry (for audit / retry).
  * Status progresses Received -> Processed | Failed | Skipped.
  * The ERPNext Error Log is reserved for real exceptions only — successful
    Sales Order creation never writes to it.
"""

import base64
import hashlib
import hmac
import json

import frappe
from frappe.utils.password import get_decrypted_password
from shopify_integration.utils.webhook import log_webhook, update_log_status
from shopify_integration.utils.sales_order import (
    create_sales_order_from_shopify,
    send_failure_email,
)
from shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings import (
    get_settings_for_store,
)


@frappe.whitelist(allow_guest=True)
def shopify_webhook():
    """
    Single webhook endpoint for all Shopify stores.
    Routes by X-Shopify-Shop-Domain header to the correct Shopify Settings record.
    HMAC-SHA256 signature verified when webhook_secret is configured in Shopify Settings.
    """
    # ── Permission bypass for webhook (allow_guest=True) ──────────────────────
    # The endpoint runs as Guest, which has no ERPNext permissions.
    # frappe.flags.ignore_permissions = True tells frappe.has_permission() to
    # always return True — it is entirely local to this request (stored on
    # frappe.local.flags, a plain Python object) and NEVER touches the session
    # store or Redis.  This is the correct Frappe pattern for background/webhook
    # operations; frappe.set_user() must NOT be used here because it calls
    # session_obj.update_session() which writes to Redis immediately and
    # corrupts every other logged-in user's session.
    _prev_ignore = frappe.flags.ignore_permissions
    try:
        frappe.flags.ignore_permissions = True

        # get_data(cache=True) reads from Werkzeug's cached buffer so we get
        # the original raw bytes even after Frappe's middleware has already
        # consumed the stream into frappe.local.form_dict.  Using .data directly
        # returns b"" at this point because the stream has been exhausted.
        raw_data = frappe.request.get_data(cache=True)
        headers  = frappe.request.headers

        topic       = headers.get("X-Shopify-Topic", "")
        shop_domain = headers.get("X-Shopify-Shop-Domain", "")

        # ── Parse payload ──────────────────────────────────────────────────────
        if raw_data:
            try:
                order_data = json.loads(raw_data)
            except Exception:
                order_data = frappe.local.form_dict.as_dict()
        else:
            order_data = frappe.local.form_dict.as_dict()

        # Strip Frappe internal keys
        for key in ["cmd", "csrf_token"]:
            order_data.pop(key, None)

        # ── Always log the event FIRST so there is always an audit row ─────────
        # This must happen before the HMAC check so that even rejected webhooks
        # appear in Shopify Log and can be retried once the secret is corrected.
        settings = get_settings_for_store(shop_domain)
        log_name = log_webhook(topic, shop_domain, order_data, status="Received")

        # ── HMAC signature verification ────────────────────────────────────────
        # Shopify signs every webhook with HMAC-SHA256 using the webhook secret.
        # We verify against raw_data (original bytes) — never re-serialised JSON
        # which can differ in key order.  Verification is skipped when
        # webhook_secret is blank (dev/test mode); shopify_settings.validate()
        # warns when the secret is empty.
        if settings and settings.get("webhook_secret"):
            shopify_hmac = headers.get("X-Shopify-Hmac-SHA256", "")

            # settings.webhook_secret is a Password fieldtype — Frappe returns
            # masked asterisks when accessed directly on the doc.  Must use
            # get_decrypted_password() to get the real plaintext secret.
            secret_raw = (
                get_decrypted_password(
                    "Shopify Settings", settings.name, "webhook_secret",
                    raise_exception=False,
                ) or ""
            ).strip()

            expected_hmac = base64.b64encode(
                hmac.new(
                    secret_raw.encode("utf-8"),
                    raw_data or b"",
                    hashlib.sha256,
                ).digest()
            ).decode("utf-8")

            if not hmac.compare_digest(shopify_hmac, expected_hmac):
                update_log_status(
                    log_name=log_name,
                    shopify_order_id=str(order_data.get("id", "")),
                    status="Failed",
                    error=f"HMAC signature mismatch. Received: {shopify_hmac}",
                )
                frappe.log_error(
                    f"HMAC mismatch for store '{shop_domain}'.\n"
                    f"Received : {shopify_hmac}\n"
                    f"Shopify Log: {log_name}\n\n"
                    f"If this is a legitimate order, open the log and click Retry Order "
                    f"after confirming the webhook_secret in Shopify Settings is correct.",
                    "Shopify: Webhook Signature Invalid"
                )
                frappe.db.commit()  # nosemgrep: frappe-manual-commit — webhook must persist log before HTTP response
                return {"status": "error", "reason": "invalid signature"}

        if not settings:
            update_log_status(
                log_name=log_name,
                shopify_order_id=str(order_data.get("id", "")),
                status="Skipped",
                error="Store not configured or sync disabled",
            )
            return {"status": "ignored", "reason": "store not configured or sync disabled"}

        # ── Route by topic ─────────────────────────────────────────────────────
        if topic == "orders/create":
            # PHASE 1 & 2: Asynchronous Execution & Lock Protection
            # Enqueue the processing instead of running it synchronously. This ensures we respond
            # to Shopify within their 5-second timeout, preventing redundant webhook retries.
            # Using job_name guarantees that if Shopify fires identical webhooks concurrently,
            # RQ will only queue one background job, perfectly eliminating race conditions.
            
            shopify_order_id = str(order_data.get("id", ""))
            job_id = f"shopify_order_{shopify_order_id}"
            
            frappe.enqueue(
                "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.retry_order",
                queue="default",
                timeout=300,
                docname=log_name,
                job_name=job_id,
                enqueue_after_commit=True
            )
            
            update_log_status(
                log_name=log_name,
                shopify_order_id=shopify_order_id,
                status="Received", # Keeps it pending until the background job runs
                error="Enqueued for background processing."
            )
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — must commit before returning HTTP 200 to Shopify
            
            return {
                "status": "ok",
                "store": shop_domain,
                "topic": topic,
                "message": "Enqueued for background processing"
            }

        # ── Unhandled topics (orders/paid, orders/cancelled, orders/fulfilled) ─
        update_log_status(
            log_name=log_name,
            shopify_order_id=str(order_data.get("id", "")),
            status="Skipped",
            error=f"Topic '{topic}' not yet handled",
        )
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — must commit before returning HTTP response
        return {"status": "skipped", "store": shop_domain, "topic": topic}

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify Webhook Error")
        return {"status": "error"}

    finally:
        # Restore the flag — keeps request scope clean for any post-processing
        # Frappe does after the view function returns.
        frappe.flags.ignore_permissions = _prev_ignore
