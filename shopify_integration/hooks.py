app_name        = "shopify_integration"
app_title       = "Shopify Integration"
app_publisher   = "Yash Chaurasia"
app_description = "Shopify to ERPNext integration with automatic Sales Orders, Payment Entries, Sales Invoices, and India GST compliance."
app_email       = "chaurasiayash351@gmail.com"
app_license     = "GPLv3"
app_version     = "1.0.0"
app_color       = "#96BF48"
app_icon        = "octicon octicon-package"

required_apps   = ["frappe", "erpnext"]

# ----------------------------------------------------------
# DocType JavaScript — loaded only when viewing that DocType
# ----------------------------------------------------------
doctype_js = {
    "Delivery Note": "public/js/delivery_note.js",
}

doctype_list_js = {
    "Delivery Note": "public/js/delivery_note_list.js",
}

# ----------------------------------------------------------
# Install / Uninstall hooks
# ----------------------------------------------------------
after_install  = "shopify_integration.install.after_install"
before_uninstall = "shopify_integration.install.before_uninstall"

# ----------------------------------------------------------
# DocType event hooks
# ----------------------------------------------------------
doc_events = {
    "Sales Order": {
        # Clear Shopify fields on amended copies so duplicate-check is not
        # blocked and manual amendments are not linked to Shopify orders.
        "before_insert": "shopify_integration.utils.sales_order.clear_shopify_fields_on_amend",
        # Clear Shopify Log reference before deletion so ERPNext link-validation
        # does not block Sales Order deletion.
        "on_trash": "shopify_integration.utils.sales_order.clear_shopify_log_on_trash",
    },
    "Delivery Note": {
        # Immediate SI creation — enqueues a background job when si_dn_timing
        # is set to "Immediate" in Shopify Settings.  No-ops for all other DNs.
        #
        # Immediate Shopify fulfillment — enqueues a background job when
        # dn_fulfillment_timing is "Immediate".  No-ops for Manual/Scheduled
        # stores and for every non-Shopify DN.  Both hooks are enqueue-only, so
        # neither can slow down or fail a Delivery Note submission.
        "on_submit": [
            "shopify_integration.utils.sales_invoice.create_si_from_dn_on_submit",
            "shopify_integration.utils.fulfillment.fulfil_on_dn_submit",
        ],
        # Cancelling a DN whose order was already fulfilled in Shopify leaves the
        # two systems disagreeing.  Depending on the store's
        # "On Delivery Note Cancel" setting this either cancels the Shopify
        # fulfillment or logs the divergence loudly.  Enqueued after commit, so
        # Shopify can never block the cancellation of a stock document.
        "on_cancel": "shopify_integration.utils.fulfillment.handle_dn_cancel",
    },
}

# ----------------------------------------------------------
# Scheduler jobs
# ----------------------------------------------------------
scheduler_events = {
    "hourly": [
        # "After Delivery Note" mode: find submitted DNs that have no SI yet
        # and create Sales Invoices for them.
        "shopify_integration.utils.scheduler.create_invoices_after_delivery_note",
        # "Scheduled" fulfillment mode: fulfil Shopify orders for DNs submitted
        # more than dn_fulfillment_delay_hours ago.  The delay is the window in
        # which a wrong DN can be cancelled before Shopify is ever told.
        # No-ops entirely while no store has enable_fulfillment set.
        "shopify_integration.utils.scheduler.fulfil_submitted_delivery_notes",
    ],
    "daily": [
        # Delete Shopify Logs older than `shopify_log_retention_days` days.
        "shopify_integration.utils.scheduler.delete_old_shopify_logs",
    ],
}

# ----------------------------------------------------------
# Whitelisted API endpoint for Shopify webhooks
# Accessed via: /api/method/shopify_integration.api.shopify_webhook
# ----------------------------------------------------------
