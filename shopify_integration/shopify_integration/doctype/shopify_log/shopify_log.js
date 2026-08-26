// shopify_log.js — Buttons on Shopify Log form.
//
// Policy: logs are retained for both success and failure.  A successful
// retry marks the log "Processed" and links the new SO; it is NOT deleted.
//
// Payload correction: `payload` is read-only and never edited — it is the record
// of what Shopify sent.  Corrections go to `corrected_payload`, which Retry
// Order prefers.  A banner makes it obvious when a retry will replay something
// other than the original.

frappe.ui.form.on("Shopify Log", {
    refresh(frm) {
        if (frm.is_new()) return;

        _correction_banner(frm);

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

        // ─── Fix Payload group ────────────────────────────────────────────
        // For logs that failed because the data Shopify sent cannot produce a
        // valid Sales Order — a wrong pincode being the usual culprit.
        frm.add_custom_button(
            __("Re-fetch from Shopify"),
            () => _refetch_payload(frm),
            __("Fix Payload"),
        );
        frm.add_custom_button(
            __("Edit Payload"),
            () => _edit_payload(frm),
            __("Fix Payload"),
        );
        if (frm.doc.corrected_payload) {
            frm.add_custom_button(
                __("Discard Correction"),
                () => _clear_correction(frm),
                __("Fix Payload"),
            );
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


// ── Banner ────────────────────────────────────────────────────────────────────

function _correction_banner(frm) {
    if (!frm.doc.corrected_payload) return;

    // Never let a corrected log look like verbatim Shopify data.
    const bits = [
        __("<b>Retry will replay the corrected payload</b>, not what Shopify sent."),
    ];
    if (frm.doc.payload_correction_status) {
        bits.push(__("Source: {0}.", [frm.doc.payload_correction_status]));
    }
    if (frm.doc.corrected_by) {
        bits.push(__("By {0}.", [frm.doc.corrected_by]));
    }
    if (frm.doc.correction_reason) {
        bits.push(frappe.utils.escape_html(frm.doc.correction_reason));
    }

    frm.dashboard.clear_headline();
    frm.dashboard.set_headline_alert(
        '<div style="padding-right:40px;display:block;line-height:1.5;">'
        + bits.join(" ") + "</div>",
        "orange",
    );
}


// ── Retry ─────────────────────────────────────────────────────────────────────

function _retry_order(frm) {
    const note = frm.doc.corrected_payload
        ? "<p>" + __("This will replay the <b>corrected</b> payload.") + "</p>"
        : "";

    frappe.confirm(
        "<p>" + __("Re-process this Shopify order and create a Sales Order?") + "</p>" + note,
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


// ── Re-fetch from Shopify ─────────────────────────────────────────────────────

function _refetch_payload(frm) {
    frappe.confirm(
        "<p>" + __("Pull this order fresh from Shopify and use it for the next retry?") + "</p>"
        + "<p class='text-muted'>"
        + __("Fix the order in Shopify first — that is where the customer's own record lives, so the correction also applies to their next order. The original payload on this log is kept.")
        + "</p>",
        () => {
            frappe.call({
                method: "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.refetch_payload_from_shopify",
                args:   { docname: frm.doc.name },
                freeze: true,
                freeze_message: __("Fetching from Shopify…"),
                callback: () => {
                    frappe.show_alert({
                        message: __("Payload re-fetched. Review it, then click Retry Order."),
                        indicator: "green",
                    });
                    frm.reload_doc();
                },
            });
        },
    );
}


// ── Edit payload ──────────────────────────────────────────────────────────────

function _edit_payload(frm) {
    frappe.call({
        method: "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.get_payload_for_edit",
        args:   { docname: frm.doc.name },
        freeze: true,
        callback: (r) => {
            const info = (r && r.message) || {};
            _show_edit_dialog(frm, info);
        },
    });
}


function _show_edit_dialog(frm, info) {
    const dialog = new frappe.ui.Dialog({
        title: __("Edit Payload"),
        size: "large",
        fields: [
            {
                fieldtype: "HTML",
                options:
                    "<p>" +
                    __("Correct the JSON below, then Save & Retry. The original payload is kept as the record of what Shopify sent.") +
                    "</p><p class='text-muted'>" +
                    __("The order id must stay the same — a correction may not move this log to a different order.") +
                    "</p>",
            },
            {
                fieldname: "payload_json",
                fieldtype: "Code",
                label: __("Payload (JSON)"),
                options: "JSON",
                default: info.payload || "",
                reqd: 1,
            },
            {
                fieldname: "reason",
                fieldtype: "Small Text",
                label: __("Reason for Correction"),
                default: info.correction_reason || "",
                description: __("e.g. Customer gave the wrong pincode; corrected to 400001."),
                reqd: 1,
            },
        ],
        primary_action_label: __("Save & Retry"),
        primary_action(values) {
            _save_correction(frm, values, true, dialog);
        },
        secondary_action_label: __("Save Only"),
        secondary_action() {
            _save_correction(frm, dialog.get_values(), false, dialog);
        },
    });

    dialog.show();
}


function _save_correction(frm, values, then_retry, dialog) {
    if (!values) return;

    frappe.call({
        method: "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.save_corrected_payload",
        args: {
            docname:      frm.doc.name,
            payload_json: values.payload_json,
            reason:       values.reason,
        },
        freeze: true,
        freeze_message: __("Saving correction…"),
        callback: () => {
            dialog.hide();
            frappe.show_alert({
                message: __("Correction saved."),
                indicator: "green",
            });
            if (then_retry) {
                // reload first so the retry reads the stored correction
                frm.reload_doc().then(() => _retry_order(frm));
            } else {
                frm.reload_doc();
            }
        },
    });
}


function _clear_correction(frm) {
    frappe.confirm(
        __("Discard the correction and go back to replaying exactly what Shopify sent?"),
        () => {
            frappe.call({
                method: "shopify_integration.shopify_integration.doctype.shopify_log.shopify_log.clear_corrected_payload",
                args:   { docname: frm.doc.name },
                callback: () => {
                    frappe.show_alert({
                        message: __("Correction discarded."),
                        indicator: "blue",
                    });
                    frm.reload_doc();
                },
            });
        },
    );
}


// ── Reset ─────────────────────────────────────────────────────────────────────

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
