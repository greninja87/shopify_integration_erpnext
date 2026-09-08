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
from shopify_integration.utils.refund_report import (
    needs_report_note,
    report_refund_from_webhook,
)
from shopify_integration.utils.webhook import log_webhook, update_log_status
from shopify_integration.utils.sales_order import create_sales_order_from_shopify
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
        #
        # For non-order topics (e.g. refunds/create), order_data["id"] is the
        # sub-resource ID (refund ID), not the Shopify order ID.  Pass the real
        # order ID explicitly so the log is searchable by Shopify order.
        _log_order_id = ""
        _log_order_name = ""
        if topic == "refunds/create":
            # Refund payloads: "id" = refund ID, "order_id" = the actual order ID.
            # "name" field (e.g. "#1042") doesn't exist on refund payloads — look it
            # up from the Sales Order so the log row is human-readable.
            _log_order_id = str(order_data.get("order_id", ""))
            if _log_order_id:
                _log_order_name = frappe.db.get_value(
                    "Sales Order",
                    {"shopify_order_id": _log_order_id},
                    "po_no",  # SO stores Shopify order name (#1042) in po_no
                ) or ""

        settings = get_settings_for_store(shop_domain)
        log_name = log_webhook(
            topic, shop_domain, order_data,
            status="Received",
            shopify_order_id=_log_order_id,
            shopify_order_name=_log_order_name,
        )

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
        if topic == "refunds/create":
            shopify_order_id = str(order_data.get("order_id", ""))
            refund_id        = str(order_data.get("id", ""))

            # ── Report the refund to whichever app records refunds ───────────
            # Independent of the credit-note settings below, and of
            # enable_refund_writeback: this moves no money and creates no ledger
            # document, it only hands over the facts payment_portals cannot see
            # (a refund that never reached Cashfree, plus the Shopify refund's
            # own note, line items, restock and staff user).  Never raises, so
            # it cannot turn a refund we already have into a webhook retry.
            #
            # Called on every refunds/create, but NOT unconditional: a refund
            # this app wrote back to Shopify itself is skipped inside
            # report_refund(), which consults the same
            # refund_request_for_shopify_refund() the credit-note path below
            # uses, and returns outcome "own_writeback" without telling
            # anybody.  Our own refundCreate fires this very webhook, so
            # without that check payment_portals would be told about a refund
            # it raised and is already booking — the second recorder for one
            # refund, which is two Payment Entries.  The check lives at that
            # seam rather than here on purpose: the backfill path crosses it
            # too, and a guard written here is a guard the backfill forgets.
            #
            # A refund on an order where a write-back of ours was posted and
            # never confirmed IS reported, carrying the Refund Request names
            # as an optional fact, because a withheld report is an omission
            # nothing recovers.  The credit-note enqueue below withholds on
            # the same evidence and says why; the two paths differ on purpose.
            #
            # Delivery is at-least-once by design.  A Shopify retry of this
            # webhook reports the same refund again, and the observer dedupes
            # on shopify_refund_id.  See utils/refund_report.py and
            # REFUND-REPORT-CONTRACT.md §5a.
            report = report_refund_from_webhook(
                order_data,
                shop_domain=shop_domain,
                shopify_order_name=_log_order_name,
            )
            # The note exists for a report that did NOT land: it puts the
            # reason on the log row a person opens.  It is deliberately NOT
            # branched on `delivered` alone — the deliberate skip is not
            # delivered either, and `own_writeback` is the EXPECTED outcome for
            # every refund this app writes back, so branching that way pasted
            # an explanatory sentence into the error_message of every one of
            # those webhooks.  An error field carrying a routine sentence is an
            # error field nobody reads.  A delivered report writes nothing here
            # either, including the one delivered while a write-back of ours
            # was unconfirmed: that one is already in the Error Log with the
            # row to resolve, which is where whoever has to act on it looks.
            # The predicate lives beside the outcome vocabulary so this cannot
            # drift from it.
            report_note = (
                f" Refund report: {report.get('message', '')}"
                if needs_report_note(report.get("outcome", "")) else ""
            )

            if settings.get("enable_sales_invoice") and settings.get("enable_credit_note"):
                if settings.get("credit_note_creation") == "Auto":
                    job_id = f"shopify_refund_{refund_id}"
                    frappe.enqueue(
                        "shopify_integration.utils.credit_note._create_credit_note_background",
                        queue="default",
                        timeout=120,
                        refund_data=order_data,
                        store_name=settings.name,
                        log_name=log_name,
                        job_name=job_id,
                        enqueue_after_commit=True,
                    )
                    update_log_status(
                        log_name=log_name,
                        shopify_order_id=shopify_order_id,
                        status="Received",
                        error="Enqueued for credit note creation." + report_note,
                    )
                else:
                    # Manual mode — log it so the user knows to act
                    update_log_status(
                        log_name=log_name,
                        shopify_order_id=shopify_order_id,
                        status="Skipped",
                        error="Credit Note creation is set to Manual — create the Credit Note yourself in ERPNext against the Sales Invoice for this order." + report_note,
                    )
            else:
                update_log_status(
                    log_name=log_name,
                    shopify_order_id=shopify_order_id,
                    status="Skipped",
                    error="Credit Note creation is not enabled for this store." + report_note,
                )

            frappe.db.commit()  # nosemgrep: frappe-manual-commit — must commit before returning HTTP 200 to Shopify
            return {"status": "ok", "store": shop_domain, "topic": topic}

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
