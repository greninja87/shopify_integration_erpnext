// shopify_log.js — Buttons on Shopify Log form.
// Policy: logs are retained for both success and failure.  A successful
// retry marks the log "Processed" and links the new SO; it is NOT deleted.

frappe.ui.form.on("Shopify Log", {
    refresh(frm) {
        if (frm.is_new()) return;

        // ─── Retry Order ──────────────────────────────────────────────────
        // Show for any log that isn't already successfully linked to a live SO.
        const already_done = (
            frm.doc.status === "Processed" && frm.doc.erpnext_sales_order
        );
        if (!already_done) {
            frm.add_custom_button(
                __("Retry Order"),
                () => _retry_order(frm),
            ).addClass("btn-primary");
        }

        // ─── Reset For Retry ──────────────────────────────────────────────
        // Clears the SO link + status so the log can be retried cleanly.
        frm.add_custom_button(
            __("Reset For Retry"),
            () => _reset_log(frm),
            __("Actions"),
        );
    },
});

function _retry_order(frm) {
    frappe.confirm(
        __("Re-process this Shopify order and create a Sales Order?"),
        () => {
            frappe.call({
                method: "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.retry_order",
                args:   { docname: frm.doc.name },
                freeze: true,
                freeze_message: __("Processing order, please wait..."),
                callback: (r) => {
                    const msg = r && r.message;
                    if (!msg) return;

                    if (msg.status === "success" && msg.sales_order) {
                        frappe.msgprint({
                            title: __("Order Processed"),
                            indicator: "green",
                            message: __(
                                "Sales Order {0} created successfully.",
                                [
                                    `<a href="/app/sales-order/${msg.sales_order}">${msg.sales_order}</a>`,
                                ],
                            ),
                        });
                    } else if (msg.status === "duplicate" && msg.sales_order) {
                        frappe.msgprint({
                            title: __("Already Exists"),
                            indicator: "orange",
                            message: __(
                                "A live Sales Order {0} already exists for this Shopify order. Cancel or delete it first if you want to retry.",
                                [
                                    `<a href="/app/sales-order/${msg.sales_order}">${msg.sales_order}</a>`,
                                ],
                            ),
                        });
                    }
                    frm.reload_doc();
                },
                error: () => {
                    frappe.msgprint({
                        title: __("Retry Failed"),
                        indicator: "red",
                        message: __(
                            "Could not process the order. The error has been saved — check the Error Details section on this form.",
                        ),
                    });
                    frm.reload_doc();
                },
            });
        },
    );
}

function _reset_log(frm) {
    frappe.call({
        method: "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.reset_log_for_retry",
        args:   { docname: frm.doc.name },
        callback: () => {
            frappe.show_alert({
                message: __("Log reset — ready for retry."),
                indicator: "blue",
            });
            frm.reload_doc();
        },
    });
}
